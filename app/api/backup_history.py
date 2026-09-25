"""
备份历史 API。

相比旧实现的修正：
- 批量删除改用 POST /batch（原 DELETE /batch 被 /{history_id} 路由吞掉，永远 422）
- 清空历史默认同时删除文件，需要保留时显式传 delete_files=false
- 恢复操作把同步任务暂停/恢复的编排收敛在一处，且保证 finally 里一定恢复
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core.config import now_beijing
from ..core.crypto import CredentialDecryptError, decrypt
from ..core.database import get_db
from ..core.errors import BadRequestError, NotFoundError, OperationFailedError
from ..core.pagination import Page, PageMeta, paginate
from ..models.database import (
    BackupHistory,
    BackupPlan,
    BackupStatus,
    BackupType,
    Database,
    SyncStatus,
    SyncTask,
    User,
)
from ..services.audit import log_operation, log_run_detached
from ..services.backup.engine import delete_backup_file
from ..services.sync import sync_manager
from .deps import client_ip, get_current_user

router = APIRouter()


# ============================================================ 模型

class BackupHistoryResponse(BaseModel):
    id: int
    backup_plan_id: int
    plan_name: Optional[str] = None
    status: str
    backup_type: str
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration: Optional[int] = None
    error_message: Optional[str] = None
    trigger: Optional[str] = None
    file_exists: bool = False
    upload_status: str = "skipped"
    upload_path: Optional[str] = None
    upload_error: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    created_at: datetime


class StatisticsResponse(BaseModel):
    total_count: int
    status_stats: dict
    recent_count: int
    total_size: int
    disk_file_count: int
    orphan_file_count: int


class RestoreRequest(BaseModel):
    target_database_id: Optional[int] = Field(
        default=None, description="恢复到指定数据库，缺省恢复到备份来源库"
    )


class BatchDeleteRequest(BaseModel):
    ids: List[int] = Field(min_length=1)
    delete_files: bool = True


class MessageResponse(BaseModel):
    message: str
    affected: Optional[int] = None


# ============================================================ 辅助

def _to_response(row: BackupHistory, plan_name: Optional[str] = None) -> BackupHistoryResponse:
    import os

    return BackupHistoryResponse(
        id=row.id,
        backup_plan_id=row.backup_plan_id,
        plan_name=plan_name,
        status=row.status.value if row.status else "pending",
        backup_type=row.backup_type.value if row.backup_type else "full",
        file_path=row.file_path,
        file_size=row.file_size,
        start_time=row.start_time,
        end_time=row.end_time,
        duration=row.duration,
        error_message=row.error_message,
        trigger=row.trigger,
        file_exists=bool(row.file_path and os.path.exists(row.file_path)),
        upload_status=row.upload_status.value if row.upload_status else "skipped",
        upload_path=row.upload_path,
        upload_error=row.upload_error,
        uploaded_at=row.uploaded_at,
        created_at=row.created_at,
    )


def _plan_names(db: Session, plan_ids: List[int]) -> dict:
    if not plan_ids:
        return {}
    rows = db.query(BackupPlan.id, BackupPlan.name).filter(BackupPlan.id.in_(plan_ids)).all()
    return {row[0]: row[1] for row in rows}


# ============================================================ 列表与统计

@router.get("/statistics", response_model=StatisticsResponse)
async def get_statistics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StatisticsResponse:
    """备份统计。额外统计磁盘上实际存在的文件，便于发现孤儿文件。"""
    import os

    total_count = db.query(func.count(BackupHistory.id)).scalar() or 0

    status_rows = (
        db.query(BackupHistory.status, func.count(BackupHistory.id))
        .group_by(BackupHistory.status)
        .all()
    )
    status_stats = {
        (status.value if hasattr(status, "value") else str(status)): count
        for status, count in status_rows
    }

    from datetime import timedelta

    seven_days_ago = now_beijing() - timedelta(days=7)
    recent_count = (
        db.query(func.count(BackupHistory.id))
        .filter(BackupHistory.created_at >= seven_days_ago)
        .scalar()
        or 0
    )

    total_size = db.query(func.sum(BackupHistory.file_size)).scalar() or 0

    histories = db.query(BackupHistory.file_path).all()
    disk_count = 0
    orphan_count = 0
    for (path,) in histories:
        if path and os.path.exists(path):
            disk_count += 1

    # 孤儿文件：目录里有 .sql 但库里没有对应记录
    from ..core.config import settings

    known = {path for (path,) in histories if path}
    if os.path.isdir(settings.BACKUP_DIR):
        try:
            for name in os.listdir(settings.BACKUP_DIR):
                if not name.endswith(".sql"):
                    continue
                full = os.path.join(settings.BACKUP_DIR, name)
                if full not in known:
                    orphan_count += 1
        except OSError:
            pass

    return StatisticsResponse(
        total_count=total_count,
        status_stats=status_stats,
        recent_count=recent_count,
        total_size=total_size,
        disk_file_count=disk_count,
        orphan_file_count=orphan_count,
    )


@router.get("/", response_model=Page[BackupHistoryResponse])
async def list_history(
    plan_id: Optional[int] = None,
    status: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Page[BackupHistoryResponse]:
    """备份历史（分页）。"""
    query = db.query(BackupHistory)

    if plan_id is not None:
        query = query.filter(BackupHistory.backup_plan_id == plan_id)
    if status:
        try:
            query = query.filter(BackupHistory.status == BackupStatus(status))
        except ValueError:
            raise BadRequestError(f"未知状态: {status}")
    if start_date:
        parsed = _parse_date(start_date)
        if parsed:
            query = query.filter(BackupHistory.created_at >= parsed)
    if end_date:
        parsed = _parse_date(end_date)
        if parsed:
            query = query.filter(BackupHistory.created_at <= parsed)

    query = query.order_by(BackupHistory.id.desc())
    items, meta = paginate(query, skip, limit)

    names = _plan_names(db, [row.backup_plan_id for row in items])
    return Page[BackupHistoryResponse](
        data=[_to_response(row, names.get(row.backup_plan_id)) for row in items],
        page=meta,
    )


def _parse_date(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


# ============================================================ 批量与清空
# 注意：/batch 必须在 /{history_id} 之前注册，否则会被路径参数路由吞掉。

@router.post("/batch", response_model=MessageResponse)
async def batch_delete(
    payload: BatchDeleteRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """批量删除备份记录（可选同时删除文件）。"""
    rows = db.query(BackupHistory).filter(BackupHistory.id.in_(payload.ids)).all()
    if not rows:
        raise NotFoundError("未找到指定的备份记录")

    removed = 0
    for row in rows:
        if payload.delete_files:
            ok, _ = delete_backup_file(row.file_path)
            if ok and row.file_path:
                removed += 1
        db.delete(row)

    log_operation(
        db, current_user, "批量删除备份记录", "backup_history", None, None,
        f"删除 {len(rows)} 条记录，{removed} 个文件", ip_address=client_ip(request),
    )
    db.commit()
    return MessageResponse(message=f"已删除 {len(rows)} 条备份记录", affected=len(rows))


@router.delete("/clear", response_model=MessageResponse)
async def clear_history(
    request: Request,
    delete_files: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """
    清空所有备份历史。

    默认删除文件：旧实现默认保留文件，导致清空后磁盘上留下一堆无法管理的孤儿文件。
    """
    rows = db.query(BackupHistory).all()
    count = len(rows)

    removed = 0
    if delete_files:
        for row in rows:
            ok, _ = delete_backup_file(row.file_path)
            if ok and row.file_path:
                removed += 1

    db.query(BackupHistory).delete()
    log_operation(
        db, current_user, "清空备份历史", "backup_history", None, None,
        f"清空 {count} 条记录，删除 {removed} 个文件", ip_address=client_ip(request),
    )
    db.commit()
    return MessageResponse(message=f"已清空 {count} 条备份历史", affected=count)


# ============================================================ 单条操作

@router.get("/{history_id}", response_model=BackupHistoryResponse)
async def get_history(
    history_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BackupHistoryResponse:
    row = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
    if row is None:
        raise NotFoundError("备份记录不存在")
    names = _plan_names(db, [row.backup_plan_id])
    return _to_response(row, names.get(row.backup_plan_id))


@router.delete("/{history_id}", response_model=MessageResponse)
async def delete_history(
    history_id: int,
    request: Request,
    delete_files: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """删除一条备份记录，默认同时删除文件。"""
    row = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
    if row is None:
        raise NotFoundError("备份记录不存在")

    if delete_files:
        delete_backup_file(row.file_path)

    db.delete(row)
    log_operation(db, current_user, "删除备份记录", "backup_history", history_id,
                  f"备份#{history_id}", ip_address=client_ip(request))
    db.commit()
    return MessageResponse(message="删除成功", affected=1)


@router.get("/{history_id}/download")
async def download_backup(
    history_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """下载备份文件。"""
    import os

    from fastapi.responses import FileResponse

    row = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
    if row is None:
        raise NotFoundError("备份记录不存在")
    if not row.file_path or not os.path.exists(row.file_path):
        raise NotFoundError("备份文件不存在或已被删除")

    return FileResponse(
        path=row.file_path,
        filename=os.path.basename(row.file_path),
        media_type="application/octet-stream",
    )


# ============================================================ 恢复

@router.post("/{history_id}/restore", response_model=MessageResponse)
async def restore_backup(
    history_id: int,
    payload: RestoreRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """
    从备份文件恢复数据库。

    恢复前暂停受影响的同步任务，恢复后无论成败都重新拉起——
    旧实现的恢复路径把这一步散在多处，失败时同步任务会永久停摆。
    """
    import os

    from ..core.config import settings
    from ..services.mysql.tools import build_import_command, run_import_from_file

    row = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
    if row is None:
        raise NotFoundError("备份记录不存在")
    if not row.file_path or not os.path.exists(row.file_path):
        raise NotFoundError("备份文件不存在或已被删除")

    plan = db.query(BackupPlan).filter(BackupPlan.id == row.backup_plan_id).first()
    if plan is None:
        raise BadRequestError("原始备份计划已删除，无法确定恢复目标；请指定目标数据库")

    if payload.target_database_id:
        target = (
            db.query(Database).filter(Database.id == payload.target_database_id).first()
        )
    else:
        target = db.query(Database).filter(Database.id == plan.database_id).first()
    if target is None:
        raise NotFoundError("目标数据库不存在")

    try:
        password = decrypt(target.password)
    except CredentialDecryptError as exc:
        raise OperationFailedError(f"目标数据库凭据不可用: {exc}")

    # 找出会受影响的运行中同步任务
    affected = (
        db.query(SyncTask)
        .filter(
            (SyncTask.source_db_id == target.id) | (SyncTask.target_db_id == target.id),
            SyncTask.status == SyncStatus.RUNNING,
        )
        .all()
    )
    affected_ids = [task.id for task in affected if sync_manager.is_active(task.id)]

    log_run_detached(
        "restore", history_id, f"备份#{history_id}", "INFO",
        f"开始恢复数据库到 {target.name} "
        f"({target.host}:{target.port}/{target.database_name})",
        f"备份文件: {row.file_path}, 操作人: {current_user.username}",
    )

    # 暂停同步任务，避免恢复期间的写入与 DDL 冲突
    paused: List[int] = []
    for task_id in affected_ids:
        try:
            await sync_manager.stop_task(task_id)
            paused.append(task_id)
        except Exception as exc:
            from loguru import logger

            logger.warning(f"[Restore] 暂停同步任务 {task_id} 失败: {exc}")

    try:
        cmd = build_import_command(
            host=target.host, port=target.port, user=target.username,
            database=target.database_name,
        )
        result = await run_import_from_file(
            cmd, password=password, input_path=row.file_path,
            timeout=settings.BACKUP_TIMEOUT,
        )

        if not result.ok:
            log_run_detached(
                "restore", history_id, f"备份#{history_id}", "ERROR",
                f"恢复失败: {result.error_summary}",
            )
            log_operation(
                db, current_user, "恢复数据库失败", "backup_history", history_id,
                f"备份#{history_id}", result.error_summary, ip_address=client_ip(request),
            )
            db.commit()
            raise OperationFailedError(f"恢复失败: {result.error_summary}")

    except OperationFailedError:
        raise
    except Exception as exc:
        log_run_detached(
            "restore", history_id, f"备份#{history_id}", "ERROR", f"恢复异常: {exc}"
        )
        raise OperationFailedError(f"恢复异常: {exc}") from exc
    finally:
        # 无论成败都要把同步任务拉回来
        for task_id in paused:
            try:
                await sync_manager.start_task(task_id)
                log_run_detached(
                    "restore", task_id, None, "INFO",
                    "恢复流程结束，已重新启动同步任务",
                )
            except Exception as exc:
                from loguru import logger

                logger.error(f"[Restore] 重新启动同步任务 {task_id} 失败: {exc}")
                log_run_detached(
                    "restore", task_id, None, "ERROR",
                    f"恢复流程结束后同步任务重启失败，需人工介入: {exc}",
                )

    log_run_detached(
        "restore", history_id, f"备份#{history_id}", "INFO",
        f"数据库恢复成功: {target.name} "
        f"({target.host}:{target.port}/{target.database_name})",
    )
    log_operation(
        db, current_user, "恢复数据库", "backup_history", history_id,
        f"备份#{history_id}",
        f"恢复到 {target.name} ({target.host}:{target.port}/{target.database_name})",
        ip_address=client_ip(request),
    )
    db.commit()

    return MessageResponse(message=f"数据库已恢复到 {target.name}")


# ============================================================ 手动上传到 OpenList

@router.post("/{history_id}/upload", response_model=MessageResponse)
async def upload_backup_to_openlist(
    history_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """
    手动把某次备份上传到 OpenList。

    上传失败是常见情况（网络、远端空间、凭据），
    因此保留手动重传入口，不必重新跑一次备份。
    """
    import asyncio
    import os

    from ..models.database import UploadStatus
    from ..services import openlist
    from ..services.openlist.client import OpenListError

    row = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
    if row is None:
        raise NotFoundError("备份记录不存在")
    if not row.file_path or not os.path.exists(row.file_path):
        raise NotFoundError("备份文件不存在或已被删除，无法上传")

    if not openlist.is_enabled():
        raise BadRequestError(
            "OpenList 上传未启用。请先在「设置」页配置并启用 OpenList 上传。"
        )

    # 该计划若指定了独立目录则优先使用
    plan = db.query(BackupPlan).filter(BackupPlan.id == row.backup_plan_id).first()
    remote_dir = plan.upload_dir if plan is not None else None

    try:
        client = openlist.build_client()
        if client is None:
            raise BadRequestError(
                "OpenList 配置不完整（缺少地址、用户名或密码），请在「设置」页补全"
            )

        row.upload_status = UploadStatus.UPLOADING
        row.upload_error = None
        db.commit()

        result = await asyncio.to_thread(client.upload_file, row.file_path, remote_dir)
    except OpenListError as exc:
        row.upload_status = UploadStatus.FAILED
        row.upload_error = str(exc)[:1000]
        log_operation(
            db, current_user, "上传备份失败", "backup_history", history_id,
            f"备份#{history_id}", str(exc)[:500], ip_address=client_ip(request),
        )
        db.commit()
        raise OperationFailedError(f"上传失败: {exc}")
    except BadRequestError:
        raise
    except Exception as exc:
        row.upload_status = UploadStatus.FAILED
        row.upload_error = str(exc)[:1000]
        db.commit()
        raise OperationFailedError(f"上传失败: {exc}")

    row.upload_status = UploadStatus.SUCCESS
    row.upload_path = result.remote_path
    row.uploaded_at = now_beijing()
    row.upload_error = None

    log_operation(
        db, current_user, "上传备份到 OpenList", "backup_history", history_id,
        f"备份#{history_id}", result.remote_path, ip_address=client_ip(request),
    )
    db.commit()

    return MessageResponse(message=f"已上传到 {result.remote_path}", affected=1)
