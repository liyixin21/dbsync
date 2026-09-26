"""
同步任务的生命周期管理。

- 进程内维护 task_id → SyncEngine 的注册表
- 启动/停止/自动恢复
- 并发上限保护
- 依赖健康检查：任务健康度按表状态与 DLQ 汇总，而不是永远报 running
"""
import asyncio
import threading
import time
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
from ..mysql.conninfo import build_connect_kwargs, connect_plain_fallback
from .checkpoint import load_checkpoint
from .engine import BinlogReader, SyncConfigError, SyncEngine, assert_reader_lib_sane

# 任务稳定运行超过该时长后，崩溃计数归零。
# 目的：区分「起来就崩的循环」与「跑了一天偶发崩一次」，
# 后者不该被累计到上限而永久失去自愈。
_RECOVER_RESET_AFTER = 600.0


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
        # 自愈状态：任务崩溃后已重启的次数，以及本轮计数的起算时刻。
        # 计数会在任务稳定运行一段时间后归零，避免「每天崩一次」累计到上限后
        # 永久停止自愈；同时又能拦住「起来就崩」的快速崩溃循环。
        self._recover_counts: Dict[int, int] = {}
        self._recover_since: Dict[int, float] = {}
        # 已排定但还没到退避时刻的重启（task_id → 截止的 monotonic 时刻）。
        # 用截止时刻而非 sleep，避免在共享的健康循环里阻塞其他任务。
        self._recover_pending: Dict[int, float] = {}

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
        self._recover_counts.clear()
        self._recover_since.clear()
        self._recover_pending.clear()

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
        """启动前校验 binlog 读取库版本、源库 binlog 配置与目标库可达性。"""
        # 先查依赖库版本：低版本会在连接抖动时崩溃，属于必须挡在门外的硬缺陷。
        try:
            assert_reader_lib_sane()
        except SyncConfigError as exc:
            raise SyncConfigErrorOut(str(exc)) from exc

        try:
            conn = connect_plain_fallback(
                **build_connect_kwargs(
                    host=source.host, port=source.port, user=source.username,
                    password=_decrypt_or_raise(source),
                )
            )
            conn.close()
        except Exception as exc:
            raise DatabaseConnectionError(f"源数据库连接失败: {exc}") from exc

        try:
            conn = connect_plain_fallback(
                **build_connect_kwargs(
                    host=target.host, port=target.port, user=target.username,
                    password=_decrypt_or_raise(target),
                )
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
            # 引擎崩溃后线程已不在运行，但库里的状态还是 failed/running。
            # 若仍按「未在运行即报错」处理，用户就既停不掉、也改不动这个任务——
            # 只能靠重启进程。这种情况允许清理。
            crashed = engine is not None and engine.has_failed
            if not crashed and (engine is None or not engine.is_running):
                raise BadRequestError("任务未在运行中")
            task_name = task.name

        # 自愈可能在等退避后重启；先取消计数，否则停掉后会被再次拉起。
        self._recover_counts.pop(task_id, None)
        self._recover_since.pop(task_id, None)
        self._recover_pending.pop(task_id, None)

        # 停机必须在工作线程外等待，避免阻塞事件循环
        if engine is not None:
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
        """周期性刷新指标，并把意外死亡的同步线程拉起来。"""
        with self._lock:
            engines = list(self._engines.items())

        for task_id, engine in engines:
            try:
                if engine._checkpoint is not None:  # noqa: SLF001
                    engine._checkpoint.maybe_flush()  # noqa: SLF001
            except Exception as exc:
                logger.error(f"刷新任务 {task_id} 指标失败: {exc}")

        # 在所有任务指标落盘之后再处理自愈：先让崩溃现场（状态/错误信息）
        # 写进库，再决定是否重启，否则前端可能永远看不到崩溃原因。
        await self._recover_dead_tasks(engines)

    async def _recover_dead_tasks(self, engines: List[tuple]) -> None:
        """
        检测并重启意外死亡的同步线程。

        「任务处于异常退出、引擎却已不在运行」只有两种可能：
        线程崩溃，或它从未真正起来。两种情况都需要拉起。
        正常 stop() 会先把引擎移出注册表，因此不会误判为崩溃。
        """
        if not self._running:
            return

        max_recover = settings.SYNC_AUTO_RECOVER_MAX
        now = time.monotonic()

        for task_id, engine in engines:
            if engine.is_running or not engine.has_failed:
                # 运行正常：累计稳定时长后清空崩溃计数，避免长期运行的任务
                # 因跨天累计的偶发崩溃而耗尽自愈额度。
                if engine.is_running:
                    since = self._recover_since.get(task_id)
                    if since is not None and now - since >= _RECOVER_RESET_AFTER:
                        self._recover_counts.pop(task_id, None)
                        self._recover_since.pop(task_id, None)
                continue

            # 已有等待中的重启计划：不要重复排队
            pending = self._recover_pending.get(task_id)
            if pending is not None:
                if now < pending:
                    continue
                # 到点：交给后台任务执行，本循环立即返回
                self._recover_pending.pop(task_id, None)
                self._spawn_recovery(task_id, engine)
                continue

            count = self._recover_counts.get(task_id, 0)
            if max_recover <= 0 or count >= max_recover:
                if count == max_recover:
                    logger.error(
                        f"同步任务 {task_id} 已连续崩溃 {count} 次，"
                        f"达到上限 {max_recover}，停止自动恢复。"
                        f"最后错误：{engine.last_error}"
                    )
                    self._recover_counts[task_id] = count + 1
                continue

            self._recover_counts[task_id] = count + 1

            delay = min(
                settings.SYNC_AUTO_RECOVER_DELAY * (2 ** count),
                settings.SYNC_RETRY_MAX_DELAY,
            )
            logger.warning(
                f"同步任务 {task_id} 异常退出（第 {count + 1}/{max_recover} 次），"
                f"{delay:.0f}s 后自动恢复。原因：{engine.last_error}"
            )
            # 只登记退避截止时刻，不在这里 sleep。
            # 这是 health 循环共用的协程，一旦在此等待几十秒，
            # 其他任务的位点落盘与健康刷新会被一起拖住。
            self._recover_pending[task_id] = now + delay

    def _spawn_recovery(self, task_id: int, engine) -> None:
        """把一次恢复放到独立任务里执行，避免拖慢健康循环。"""

        async def runner() -> None:
            try:
                await self._restart_dead_task(task_id)
                self._recover_since[task_id] = time.monotonic()
            except Exception as exc:
                logger.error(f"自动恢复同步任务 {task_id} 失败: {exc}")
                with session_scope() as db:
                    row = db.query(SyncTask).filter(SyncTask.id == task_id).first()
                    if row:
                        row.status = SyncStatus.FAILED
                        row.health = SyncHealth.STALLED
                        row.error_message = f"自动恢复失败: {exc}"[:1000]

        try:
            asyncio.get_running_loop().create_task(runner())
        except RuntimeError:
            # 无事件循环（如单元测试直接调用）：同步执行，保证行为一致
            asyncio.run(runner())

    async def _restart_dead_task(self, task_id: int) -> None:
        """重启一个已崩溃的任务，位点从库中最后已提交处继续。"""
        with self._lock:
            self._engines.pop(task_id, None)

        with session_scope() as db:
            task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
            if task is None:
                return
            task_name = task.name
            status = task.status
            # 用户按过停止：状态会被写成 stopped/paused，此时不该复活它。
            # 崩溃写的是 failed，属于要恢复的情形。
            if status in (SyncStatus.STOPPED, SyncStatus.PAUSED, SyncStatus.PENDING):
                logger.info(
                    f"同步任务 {task_id} 状态为 {status.value}（用户已停止），不再自动恢复"
                )
                return

        await self.start_task(task_id)
        task_log(
            "sync", task_id, task_name, "WARNING",
            "同步线程异常退出，已自动恢复（位点从上次提交处继续）",
        )
        logger.info(f"同步任务 {task_id} 已自动恢复")


sync_manager = SyncManager()
