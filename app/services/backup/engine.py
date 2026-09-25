"""
全量备份执行器。

增量备份已移除。相比旧实现的变化：

- 子进程全程异步，不再在事件循环里跑 subprocess.run(timeout=3600)。
- 先写临时文件，校验完整后原子改名——半截文件绝不会被当成有效备份。
- 保留 N 份的清理逻辑基于「已完成的备份」，并且在删除文件前先确认记录。
"""
import asyncio
import os
import shutil
from dataclasses import dataclass
from typing import Optional, Tuple

from loguru import logger

from ...core.config import now_beijing, settings
from ...core.database import session_scope
from ...models.database import (
    BackupHistory,
    BackupPlan,
    BackupStatus,
    BackupType,
    Database,
    UploadStatus,
)
from ..audit import task_log
from ..mysql.tools import (
    build_dump_command,
    run_dump_to_file,
    verify_dump_file,
)

TMP_SUFFIX = ".partial"


@dataclass
class BackupOutcome:
    """备份执行结果。"""

    success: bool
    history_id: int
    file_path: Optional[str] = None
    file_size: int = 0
    duration: int = 0
    error: Optional[str] = None


class BackupEngine:
    """执行一次全量备份。无状态，可并发实例化。"""

    def __init__(
        self,
        *,
        plan_id: int,
        database: Database,
        retention_count: int = 50,
        upload_enabled: bool = False,
        upload_dir: Optional[str] = None,
    ) -> None:
        self.plan_id = plan_id
        self.database = database
        self.retention_count = retention_count
        self.upload_enabled = upload_enabled
        self.upload_dir = upload_dir

    # ------------------------------------------------------------ OpenList 上传

    async def _upload_to_openlist(self, history_id: int, local_path: str) -> None:
        """
        备份完成后上传到 OpenList。

        上传失败不会让备份本身变成失败：本地文件已落盘并通过完整性校验，
        因远端问题丢弃一次有效备份没有道理。失败原因写入历史记录，
        界面上可看到并手动重传。
        """
        if not self.upload_enabled:
            self._mark_upload(history_id, UploadStatus.SKIPPED)
            return

        # 全局开关关闭时同样跳过，并说明原因
        try:
            from ..openlist import build_client, is_enabled
        except Exception as exc:  # pragma: no cover - 导入失败属环境问题
            self._mark_upload(history_id, UploadStatus.FAILED, error=f"OpenList 模块不可用: {exc}")
            return

        if not is_enabled():
            self._mark_upload(
                history_id, UploadStatus.SKIPPED,
                error="计划已开启上传，但全局 OpenList 开关未启用（见「设置」页）",
            )
            task_log(
                "backup", self.plan_id, None, "WARNING",
                "备份已跳过上传：全局 OpenList 开关未启用",
            )
            return

        self._mark_upload(history_id, UploadStatus.UPLOADING)

        try:
            client = build_client()
            if client is None:
                raise RuntimeError("OpenList 配置不完整（缺少地址、用户名或密码）")

            # httpx 的同步客户端放在线程里跑，避免阻塞事件循环
            result = await asyncio.to_thread(
                client.upload_file, local_path, self.upload_dir
            )
        except Exception as exc:
            message = str(exc)[:1000]
            logger.error(f"[Backup] 计划 {self.plan_id} 上传失败: {message}")
            self._mark_upload(history_id, UploadStatus.FAILED, error=message)
            task_log("backup", self.plan_id, None, "ERROR", f"备份上传失败: {message}")
            return

        logger.info(f"[Backup] 计划 {self.plan_id} 上传完成: {result.remote_path}")
        self._mark_upload(history_id, UploadStatus.SUCCESS, remote_path=result.remote_path)
        task_log(
            "backup", self.plan_id, None, "INFO",
            f"备份已上传到 OpenList：{result.remote_path}",
        )

    def _mark_upload(
        self,
        history_id: int,
        status: UploadStatus,
        *,
        remote_path: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """更新历史记录中的上传状态。失败只记日志，不影响备份主流程。"""
        try:
            with session_scope() as db:
                row = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
                if row is None:
                    return
                row.upload_status = status
                if remote_path:
                    row.upload_path = remote_path
                if error:
                    row.upload_error = error
                if status is UploadStatus.SUCCESS:
                    row.uploaded_at = now_beijing()
                    row.upload_error = None
        except Exception as exc:
            logger.error(f"更新上传状态失败: {exc}")

    # ------------------------------------------------------------ 主流程

    async def run(self, *, trigger: str = "schedule") -> BackupOutcome:
        """执行一次全量备份。"""
        history_id = self._create_history(trigger)
        started = now_beijing()
        target_path: Optional[str] = None
        tmp_path: Optional[str] = None

        try:
            target_path, tmp_path = self._plan_paths()

            from ...core.crypto import CredentialDecryptError, decrypt

            try:
                password = decrypt(self.database.password)
            except CredentialDecryptError as exc:
                raise RuntimeError(f"数据库凭据不可用: {exc}") from exc

            cmd = build_dump_command(
                host=self.database.host,
                port=self.database.port,
                user=self.database.username,
                database=self.database.database_name,
            )

            logger.info(
                f"[Backup] 计划 {self.plan_id} 开始全量备份 "
                f"{self.database.name} ({self.database.host}:{self.database.port})"
            )
            task_log("backup", self.plan_id, None, "INFO", "开始执行全量备份")

            result = await run_dump_to_file(
                cmd,
                password=password,
                output_path=tmp_path,
                timeout=settings.BACKUP_TIMEOUT,
            )

            if not result.ok:
                raise RuntimeError(f"mysqldump 执行失败: {result.error_summary}")

            ok, reason = verify_dump_file(tmp_path)
            if not ok:
                raise RuntimeError(f"备份文件校验失败: {reason}")

            # 校验通过后原子改名，避免半截文件被后续流程当作有效备份
            os.replace(tmp_path, target_path)
            tmp_path = None

            size = os.path.getsize(target_path)
            duration = int((now_beijing() - started).total_seconds())

            self._finish_history(history_id, BackupStatus.COMPLETED, target_path, size, duration)
            self._cleanup_old_backups()

            task_log(
                "backup", self.plan_id, None, "INFO",
                f"全量备份完成，文件大小 {size} 字节，耗时 {duration}s",
            )
            logger.info(f"[Backup] 计划 {self.plan_id} 备份完成: {target_path} ({size} 字节)")

            # 上传到 OpenList。
            # 上传失败不回滚备份——本地文件已经落盘且校验通过，
            # 因网络问题丢弃一次有效备份没有道理。失败原因单独记录，
            # 可在界面上看到并手动重传。
            await self._upload_to_openlist(history_id, target_path)

            return BackupOutcome(
                success=True, history_id=history_id, file_path=target_path,
                file_size=size, duration=duration,
            )

        except Exception as exc:
            duration = int((now_beijing() - started).total_seconds())
            message = str(exc)[:2000]
            logger.error(f"[Backup] 计划 {self.plan_id} 备份失败: {message}")
            self._finish_history(history_id, BackupStatus.FAILED, None, 0, duration, message)
            task_log("backup", self.plan_id, None, "ERROR", f"全量备份失败: {message}")
            return BackupOutcome(
                success=False, history_id=history_id, duration=duration, error=message
            )
        finally:
            # 清理可能残留的临时文件
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # ------------------------------------------------------------ 路径

    def _plan_paths(self) -> Tuple[str, str]:
        """返回 (正式路径, 临时路径)。"""
        backup_dir = settings.BACKUP_DIR
        os.makedirs(backup_dir, exist_ok=True)

        timestamp = now_beijing().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(
            c for c in self.database.database_name if c.isalnum() or c in ("_", "-")
        ) or "db"
        filename = f"full_backup_{safe_name}_{timestamp}.sql"
        target = os.path.join(backup_dir, filename)
        return target, target + TMP_SUFFIX

    # ------------------------------------------------------------ 历史记录

    def _create_history(self, trigger: str) -> int:
        with session_scope() as db:
            history = BackupHistory(
                backup_plan_id=self.plan_id,
                status=BackupStatus.RUNNING,
                backup_type=BackupType.FULL,
                start_time=now_beijing(),
                trigger=trigger,
            )
            db.add(history)
            db.flush()
            return history.id

    def _finish_history(
        self,
        history_id: int,
        status: BackupStatus,
        file_path: Optional[str],
        file_size: int,
        duration: int,
        error: Optional[str] = None,
    ) -> None:
        with session_scope() as db:
            history = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
            if history is None:
                return
            history.status = status
            history.end_time = now_beijing()
            history.duration = duration
            if file_path:
                history.file_path = file_path
                history.file_size = file_size
            if error:
                history.error_message = error

    # ------------------------------------------------------------ 清理

    def _cleanup_old_backups(self) -> None:
        """
        只保留最近 N 个成功备份。

        先按时间倒序取出全部成功记录，超出部分逐个删除文件与记录；
        删除文件失败时保留记录，避免产生无法追溯的孤儿文件。
        """
        if not self.retention_count or self.retention_count <= 0:
            return

        with session_scope() as db:
            records = (
                db.query(BackupHistory)
                .filter(
                    BackupHistory.backup_plan_id == self.plan_id,
                    BackupHistory.status == BackupStatus.COMPLETED,
                )
                .order_by(BackupHistory.end_time.desc(), BackupHistory.id.desc())
                .all()
            )
            if len(records) <= self.retention_count:
                return

            removed = 0
            for record in records[self.retention_count:]:
                can_delete = True
                if record.file_path and os.path.exists(record.file_path):
                    try:
                        os.unlink(record.file_path)
                    except OSError as exc:
                        logger.warning(f"删除备份文件失败，保留记录: {record.file_path} - {exc}")
                        can_delete = False
                if can_delete:
                    db.delete(record)
                    removed += 1

            if removed:
                logger.info(
                    f"备份计划 {self.plan_id} 已清理 {removed} 条旧备份"
                    f"（保留 {self.retention_count}）"
                )


def delete_backup_file(path: Optional[str]) -> Tuple[bool, str]:
    """删除备份文件。返回 (删除成功或文件不存在, 说明)。"""
    if not path:
        return True, "无文件"
    if not os.path.exists(path):
        return True, "文件已不存在"
    try:
        os.unlink(path)
        return True, "已删除"
    except OSError as exc:
        return False, str(exc)


def discard_plan_files(plan_id: int) -> int:
    """删除某个计划下的全部备份文件，返回删除数量。"""
    with session_scope() as db:
        records = (
            db.query(BackupHistory)
            .filter(BackupHistory.backup_plan_id == plan_id)
            .all()
        )
        paths = [r.file_path for r in records if r.file_path]

    removed = 0
    for path in paths:
        ok, _ = delete_backup_file(path)
        if ok and path and os.path.exists(path):
            removed += 1
    return removed
