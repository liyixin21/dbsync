"""
同步任务的生命周期管理。

- 进程内维护 task_id → SyncEngine 的注册表
- 启动/停止/自动恢复
- 并发上限保护
- 依赖健康检查：任务健康度按表状态与 DLQ 汇总，而不是永远报 running
"""
import asyncio
import threading
from typing import Dict, List, Optional

from loguru import logger

from ...core.config import settings
from ...core.crypto import CredentialDecryptError, decrypt
from ...core.database import session_scope
from ...core.errors import (
    BadRequestError,
    CredentialError,
    DatabaseConnectionError,
    NotFoundError,
    SyncCapacityError,
    SyncConfigError as SyncConfigErrorOut,
)
from ...models.database import Database, SyncHealth, SyncStatus, SyncTask
from ..audit import task_log
from .checkpoint import load_checkpoint
from .engine import BinlogReader, SyncConfigError, SyncEngine


def _source_config(db: Database) -> dict:
    return {
        "host": db.host,
        "port": db.port,
        "user": db.username,
        "password": _decrypt_or_raise(db),
        "database": db.database_name,
    }


def _target_config(db: Database) -> dict:
    # database 键必须带上：引擎据此确定写入落点的库名。
    # 同实例跨库同步时它可能不同于源库名；缺失会让落点退化为源库，原地写回。
    return {
        "host": db.host,
        "port": db.port,
        "user": db.username,
        "password": _decrypt_or_raise(db),
        "database": db.database_name,
    }


def _decrypt_or_raise(db: Database) -> str:
    try:
        return decrypt(db.password)
    except CredentialDecryptError as exc:
        raise CredentialError(f"数据库「{db.name}」凭据不可用：{exc}") from exc


class SyncManager:
    """同步任务的守护与编排。"""

    def __init__(self) -> None:
        self._engines: Dict[int, SyncEngine] = {}
        self._lock = threading.RLock()
        self._running = False

    # ------------------------------------------------------------ 查询

    @property
    def running_count(self) -> int:
        with self._lock:
            return sum(1 for e in self._engines.values() if e.is_running)

    def get_engine(self, task_id: int) -> Optional[SyncEngine]:
        with self._lock:
            return self._engines.get(task_id)

    def is_active(self, task_id: int) -> bool:
        engine = self.get_engine(task_id)
        return engine.is_running if engine else False

    # ------------------------------------------------------------ 启停

    async def start(self) -> None:
        """服务启动：恢复标记为自动启动或上次处于运行态的任务。"""
        self._running = True
        logger.info("同步服务已启动")
        await self._recover_tasks()

    async def stop(self) -> None:
        """服务停止：优雅停机，确保位点落盘。"""
        self._running = False
        with self._lock:
            engines = list(self._engines.values())
            self._engines.clear()

        for engine in engines:
            try:
                await asyncio.to_thread(engine.stop)
            except Exception as exc:
                logger.error(f"停止同步任务 {engine.task_id} 出错: {exc}")
        logger.info("同步服务已停止")

    async def start_task(self, task_id: int) -> SyncTask:
        """启动一个同步任务。"""
        if self.is_active(task_id):
            raise BadRequestError("任务已在运行中")

        if self.running_count >= settings.SYNC_MAX_TASKS:
            raise SyncCapacityError(
                f"并发同步任务已达上限 {settings.SYNC_MAX_TASKS}，"
                "请先停止其他任务或调高 SYNC_MAX_TASKS"
            )

        with session_scope() as db:
            task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
            if task is None:
                raise NotFoundError("同步任务不存在")
            source = db.query(Database).filter(Database.id == task.source_db_id).first()
            target = db.query(Database).filter(Database.id == task.target_db_id).first()
            if source is None or target is None:
                raise NotFoundError("源数据库或目标数据库不存在")
            if not source.is_active or not target.is_active:
                raise BadRequestError("源数据库或目标数据库已被禁用")

            try:
                src_cfg = _source_config(source)
                tgt_cfg = _target_config(target)
            except CredentialError:
                raise

            Initial = load_checkpoint(task)
            server_id = task.server_id
            task_name = task.name

        # 分配的 server_id 固定下来，便于排查
        if not server_id:
            import random

            server_id = random.randint(
                settings.SYNC_SERVER_ID_BASE,
                settings.SYNC_SERVER_ID_BASE + settings.SYNC_SERVER_ID_RANGE,
            )
            with session_scope() as db:
                row = db.query(SyncTask).filter(SyncTask.id == task_id).first()
                if row:
                    row.server_id = server_id

        engine = SyncEngine(
            task_id=task_id,
            task_name=task_name,
            source_config=src_cfg,
            target_config=tgt_cfg,
            initial={
                "file": Initial.binlog_file,
                "pos": Initial.binlog_pos,
                "gtid": Initial.gtid_set,
            },
            server_id=server_id,
        )

        # 先做一次同步的连接与配置校验，让失败在启动阶段就暴露，
        # 而不是等线程跑起来才在日志里烂掉。
        await asyncio.to_thread(self._preflight, engine, source, target)

        with self._lock:
            self._engines[task_id] = engine

        engine.start()

        with session_scope() as db:
            row = db.query(SyncTask).filter(SyncTask.id == task_id).first()
            if row:
                row.status = SyncStatus.RUNNING
                row.health = SyncHealth.UNKNOWN
                row.error_message = None

        task_log("sync", task_id, task_name, "INFO", "同步任务已启动")
        logger.info(f"同步任务 {task_id} ({task_name}) 启动成功")

        with session_scope() as db:
            return db.query(SyncTask).filter(SyncTask.id == task_id).first()

    def _preflight(self, engine: SyncEngine, source: Database, target: Database) -> None:
        """启动前校验源库 binlog 与目标库可达性。"""
        import mysql.connector

        try:
            conn = mysql.connector.connect(
                host=source.host, port=source.port, user=source.username,
                password=_decrypt_or_raise(source), connect_timeout=10,
                connection_timeout=10, ssl_disabled=True,
            )
            conn.close()
        except Exception as exc:
            raise DatabaseConnectionError(f"源数据库连接失败: {exc}") from exc

        try:
            conn = mysql.connector.connect(
                host=target.host, port=target.port, user=target.username,
                password=_decrypt_or_raise(target), connect_timeout=10,
                connection_timeout=10, ssl_disabled=True,
            )
            conn.close()
        except Exception as exc:
            raise DatabaseConnectionError(f"目标数据库连接失败: {exc}") from exc

        caps = engine._reader.read_server_capabilities()  # noqa: SLF001
        fmt = caps.get("format")
        row_image = caps.get("row_image")
        if fmt and fmt != "ROW":
            raise SyncConfigErrorOut(
                f"源库 binlog_format 为 {fmt}，必须为 ROW。"
                "当前配置下 DML 会以 statement 记录而无法复制，导致数据静默丢失。"
                "请在源库设置 SET GLOBAL binlog_format='ROW' 并写入配置文件持久化。"
            )
        if row_image and row_image != "FULL":
            raise SyncConfigErrorOut(
                f"源库 binlog_row_image 为 {row_image}，必须为 FULL。"
                "MINIMAL 模式下 binlog 只含主键，UPDATE 会缺失 SET 字段。"
                "请在源库设置 SET GLOBAL binlog_row_image='FULL'。"
            )

    async def stop_task(self, task_id: int) -> SyncTask:
        """停止一个同步任务。"""
        engine = self.get_engine(task_id)
        with session_scope() as db:
            task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
            if task is None:
                raise NotFoundError("同步任务不存在")
            if engine is None or not engine.is_running:
                raise BadRequestError("任务未在运行中")
            task_name = task.name

        # 停机必须在工作线程外等待，避免阻塞事件循环
        await asyncio.to_thread(engine.stop)

        with self._lock:
            self._engines.pop(task_id, None)

        with session_scope() as db:
            row = db.query(SyncTask).filter(SyncTask.id == task_id).first()
            if row:
                row.status = SyncStatus.STOPPED
                row.health = SyncHealth.UNKNOWN
                row.sync_delay = 0

        task_log("sync", task_id, task_name, "INFO", "同步任务已停止")
        logger.info(f"同步任务 {task_id} 已停止")

        with session_scope() as db:
            return db.query(SyncTask).filter(SyncTask.id == task_id).first()

    async def remove_task(self, task_id: int) -> None:
        """删除任务前先停下它。"""
        engine = self.get_engine(task_id)
        if engine is not None:
            await asyncio.to_thread(engine.stop)
            with self._lock:
                self._engines.pop(task_id, None)

    # ------------------------------------------------------------ 恢复

    async def _recover_tasks(self) -> None:
        """
        启动时恢复任务。

        只恢复显式设置了 auto_start 或上次处于 running 的任务；
        其余保持停止——进程重启不该悄悄把所有任务拉起来。
        """
        with session_scope() as db:
            tasks = (
                db.query(SyncTask)
                .filter(
                    (SyncTask.auto_start == True)  # noqa: E712
                    | (SyncTask.status == SyncStatus.RUNNING)
                )
                .all()
            )
            ids = [(t.id, t.name) for t in tasks]

        if not ids:
            logger.info("没有需要恢复的同步任务")
            return

        logger.info(f"发现 {len(ids)} 个待恢复的同步任务")
        for task_id, name in ids:
            try:
                await self.start_task(task_id)
            except Exception as exc:
                logger.error(f"恢复同步任务 {task_id} ({name}) 失败: {exc}")
                with session_scope() as db:
                    row = db.query(SyncTask).filter(SyncTask.id == task_id).first()
                    if row:
                        row.status = SyncStatus.FAILED
                        row.health = SyncHealth.STALLED
                        row.error_message = str(exc)[:1000]

    # ------------------------------------------------------------ 健康汇总

    async def refresh_health(self) -> None:
        """周期性把运行时指标同步进库，供前端展示。"""
        with self._lock:
            engines = list(self._engines.items())

        for task_id, engine in engines:
            try:
                if engine._checkpoint is not None:  # noqa: SLF001
                    engine._checkpoint.maybe_flush()  # noqa: SLF001
            except Exception as exc:
                logger.error(f"刷新任务 {task_id} 指标失败: {exc}")


sync_manager = SyncManager()
