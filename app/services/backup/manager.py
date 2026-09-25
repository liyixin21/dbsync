"""
备份计划管理。

调度由 APScheduler 单例承担。旧实现每个计划起一个线程 + every(1).seconds 忙轮询，
10 个计划就是 10 个每秒唤醒的线程；现在所有计划共享一个调度器线程池。
"""
import threading
from typing import Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ...core.config import now_beijing
from ...core.database import session_scope
from ...core.errors import BadRequestError, NotFoundError
from ...models.database import BackupHistory, BackupPlan, BackupStatus, Database
from ..audit import task_log
from .engine import BackupEngine

JOB_PREFIX = "backup-plan-"


class BackupManager:
    """备份计划的生命周期与调度。"""

    def __init__(self) -> None:
        self._scheduler: Optional[BackgroundScheduler] = None
        self._lock = threading.RLock()
        self._running = False
        # 正在执行中的计划，防止同一计划并发跑多次备份
        self._inflight: Dict[int, str] = {}

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        self._running = True
        with self._lock:
            self._scheduler = BackgroundScheduler(
                job_defaults={
                    "coalesce": True,          # 错过的多次触发合并为一次
                    "max_instances": 1,        # 同一计划不并发
                    "misfire_grace_time": 300,
                }
            )
            self._scheduler.start()
        logger.info("备份服务已启动")
        await self._load_active_plans()

    async def stop(self) -> None:
        self._running = False
        with self._lock:
            scheduler, self._scheduler = self._scheduler, None
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception as exc:
                logger.error(f"关闭调度器出错: {exc}")
        logger.info("备份服务已停止")

    # ------------------------------------------------------------ 调度

    def _job_id(self, plan_id: int) -> str:
        return f"{JOB_PREFIX}{plan_id}"

    def _schedule(self, plan_id: int, interval_minutes: int) -> None:
        if self._scheduler is None:
            return
        self._scheduler.add_job(
            self._run_plan_sync,
            trigger=IntervalTrigger(minutes=max(1, interval_minutes)),
            id=self._job_id(plan_id),
            replace_existing=True,
            args=[plan_id],
        )
        next_run = None
        job = self._scheduler.get_job(self._job_id(plan_id))
        if job is not None and job.next_run_time is not None:
            # APScheduler 返回带时区的时间，转成北京时间 naive
            next_run = job.next_run_time.astimezone(now_beijing().astimezone().tzinfo)
            next_run = next_run.replace(tzinfo=None)

        with session_scope() as db:
            row = db.query(BackupPlan).filter(BackupPlan.id == plan_id).first()
            if row is not None:
                row.next_run_at = next_run

        logger.info(f"备份计划 {plan_id} 已排程，每 {interval_minutes} 分钟执行一次")

    def _unschedule(self, plan_id: int) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(self._job_id(plan_id))
        except Exception:
            pass
        with session_scope() as db:
            row = db.query(BackupPlan).filter(BackupPlan.id == plan_id).first()
            if row is not None:
                row.next_run_at = None

    async def _load_active_plans(self) -> None:
        """加载启动时处于启用状态的计划。"""
        with session_scope() as db:
            plans = db.query(BackupPlan).filter(BackupPlan.is_active == True).all()  # noqa: E712
            items = [(p.id, p.name, p.schedule_interval) for p in plans]

        if not items:
            logger.info("没有启用的备份计划")
            return

        logger.info(f"发现 {len(items)} 个启用的备份计划")
        for plan_id, name, interval in items:
            try:
                self._schedule(plan_id, interval)
            except Exception as exc:
                logger.error(f"加载备份计划 {plan_id} ({name}) 失败: {exc}")

    # ------------------------------------------------------------ 执行

    def _run_plan_sync(self, plan_id: int) -> None:
        """
        调度器回调（运行在线程池里）。

        备份是阻塞型子进程，这里用独立线程 + 独立事件循环执行异步引擎，
        不污染主事件循环。
        """
        import asyncio

        if plan_id in self._inflight:
            logger.warning(f"备份计划 {plan_id} 上一次执行尚未结束，跳过本次触发")
            return

        self._inflight[plan_id] = "running"
        try:
            asyncio.run(self._execute(plan_id, trigger="schedule"))
        except Exception as exc:
            logger.error(f"备份计划 {plan_id} 执行异常: {exc}")
        finally:
            self._inflight.pop(plan_id, None)

    async def _execute(self, plan_id: int, trigger: str) -> Optional[int]:
        """取配置并执行一次备份。返回 history_id。"""
        with session_scope() as db:
            plan = db.query(BackupPlan).filter(BackupPlan.id == plan_id).first()
            if plan is None:
                logger.error(f"备份计划 {plan_id} 不存在")
                return None
            database = db.query(Database).filter(Database.id == plan.database_id).first()
            if database is None:
                logger.error(f"备份计划 {plan_id} 关联的数据库不存在")
                task_log("backup", plan_id, plan.name, "ERROR", "关联的数据库不存在")
                return None
            retention = plan.retention_count
            plan_name = plan.name
            upload_enabled = bool(plan.upload_enabled)
            upload_dir = plan.upload_dir

            # 分离出脱离会话的对象，供后台使用
            db.expunge(database)

        engine = BackupEngine(
            plan_id=plan_id,
            database=database,
            retention_count=retention,
            upload_enabled=upload_enabled,
            upload_dir=upload_dir,
        )
        outcome = await engine.run(trigger=trigger)

        with session_scope() as db:
            row = db.query(BackupPlan).filter(BackupPlan.id == plan_id).first()
            if row is not None:
                row.last_run_at = now_beijing()

        if not outcome.success:
            logger.error(f"备份计划 {plan_id} ({plan_name}) 执行失败: {outcome.error}")
        return outcome.history_id

    async def execute_now(self, plan_id: int) -> int:
        """手动立即执行一次备份。"""
        with session_scope() as db:
            plan = db.query(BackupPlan).filter(BackupPlan.id == plan_id).first()
            if plan is None:
                raise NotFoundError("备份计划不存在")

        if plan_id in self._inflight:
            raise BadRequestError("该计划正在执行备份，请稍后再试")

        self._inflight[plan_id] = "manual"
        try:
            history_id = await self._execute(plan_id, trigger="manual")
        finally:
            self._inflight.pop(plan_id, None)

        if history_id is None:
            raise BadRequestError("备份执行失败，请查看运行日志")
        return history_id

    # ------------------------------------------------------------ 计划变更

    def on_plan_created(self, plan_id: int, is_active: bool, interval: int) -> None:
        if is_active:
            self._schedule(plan_id, interval)

    def on_plan_updated(
        self, plan_id: int, is_active: bool, interval: int, schedule_changed: bool
    ) -> None:
        if not is_active:
            self._unschedule(plan_id)
            return
        if schedule_changed or self._scheduler is None or self._scheduler.get_job(self._job_id(plan_id)) is None:
            self._schedule(plan_id, interval)

    def on_plan_deleted(self, plan_id: int) -> None:
        self._unschedule(plan_id)

    @property
    def scheduler_running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    def is_inflight(self, plan_id: int) -> bool:
        return plan_id in self._inflight


backup_manager = BackupManager()


def plan_is_running(plan_id: int) -> bool:
    """该计划是否有正在执行的备份。"""
    return backup_manager.is_inflight(plan_id)


def last_successful_backup(plan_id: int) -> Optional[BackupHistory]:
    """该计划最近一次成功的备份记录。"""
    with session_scope() as db:
        return (
            db.query(BackupHistory)
            .filter(
                BackupHistory.backup_plan_id == plan_id,
                BackupHistory.status == BackupStatus.COMPLETED,
            )
            .order_by(BackupHistory.end_time.desc())
            .first()
        )


def plan_history_count(plan_id: int) -> int:
    with session_scope() as db:
        return db.query(BackupHistory).filter(BackupHistory.backup_plan_id == plan_id).count()


def all_plan_ids() -> List[int]:
    with session_scope() as db:
        return [row.id for row in db.query(BackupPlan.id).all()]
