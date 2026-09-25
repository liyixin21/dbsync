"""
审计与运行日志的统一写入入口。

旧实现把这套逻辑在 sync_service 与 backup_service 中各抄两遍，且语义不一致
（一处的 _update_task_status 写 last_sync_time，另一处不写）。此处为唯一实现。

运行日志采用缓冲批量写入：binlog 消费每 100 条事件同步写一次库的旧做法，
会在高吞吐下把消费线程拖慢一个数量级。
"""
import atexit
import threading
import time
from typing import List, Optional, Sequence

from loguru import logger
from sqlalchemy.orm import Session

from ..core.database import SessionLocal
from ..models.database import LoginLog, OperationLog, RunLog, User

_FLUSH_INTERVAL = 0.5
_FLUSH_BATCH = 200


# ============================================================ 直接写入

def log_operation(
    db: Session,
    user: Optional[User],
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[int] = None,
    resource_name: Optional[str] = None,
    detail: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> None:
    """记录操作日志。随调用方事务一起提交。"""
    try:
        db.add(
            OperationLog(
                user_id=user.id if user else None,
                username=user.username if user else None,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_name=resource_name,
                detail=detail,
                ip_address=ip_address,
            )
        )
    except Exception as exc:
        logger.error(f"写入操作日志失败: {exc}")


def log_login(
    db: Session,
    username: str,
    ip_address: Optional[str],
    user_agent: Optional[str],
    success: bool,
    failure_reason: Optional[str] = None,
) -> None:
    """记录登录日志。"""
    try:
        db.add(
            LoginLog(
                username=username[:50],
                ip_address=(ip_address or "")[:64],
                user_agent=(user_agent or "")[:500],
                success=success,
                failure_reason=(failure_reason or "")[:200] or None,
            )
        )
    except Exception as exc:
        logger.error(f"写入登录日志失败: {exc}")


def log_run(
    db: Session,
    task_type: str,
    task_id: int,
    task_name: Optional[str],
    level: str,
    message: str,
    detail: Optional[str] = None,
) -> None:
    """同步写入单条运行日志。低频场景使用。"""
    try:
        db.add(
            RunLog(
                task_type=task_type,
                task_id=task_id,
                task_name=(task_name or "")[:100] or None,
                level=level.upper(),
                message=message,
                detail=detail,
            )
        )
    except Exception as exc:
        logger.error(f"写入运行日志失败: {exc}")


def log_run_detached(
    task_type: str,
    task_id: int,
    task_name: Optional[str],
    level: str,
    message: str,
    detail: Optional[str] = None,
) -> None:
    """在独立会话中写入一条运行日志（后台线程使用）。"""
    db = SessionLocal()
    try:
        log_run(db, task_type, task_id, task_name, level, message, detail)
        db.commit()
    except Exception as exc:
        logger.error(f"写入运行日志失败: {exc}")
    finally:
        db.close()


# ============================================================ 缓冲写入

class _RunLogBuffer:
    """运行日志缓冲。由后台线程定期 flush。"""

    def __init__(self) -> None:
        self._rows: List[tuple] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._loop, name="runlog-flusher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        self.flush()

    def enqueue(
        self,
        task_type: str,
        task_id: int,
        task_name: Optional[str],
        level: str,
        message: str,
        detail: Optional[str] = None,
    ) -> None:
        row = (
            task_type,
            task_id,
            (task_name or "")[:100] or None,
            level.upper(),
            message,
            detail,
        )
        with self._lock:
            self._rows.append(row)
            should_flush = len(self._rows) >= _FLUSH_BATCH
        if should_flush:
            self.flush()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(_FLUSH_INTERVAL)
            try:
                self.flush()
            except Exception as exc:  # pragma: no cover
                logger.error(f"运行日志 flush 异常: {exc}")

    def flush(self) -> int:
        with self._lock:
            if not self._rows:
                return 0
            rows, self._rows = self._rows, []

        db = SessionLocal()
        try:
            db.bulk_save_objects(
                [
                    RunLog(
                        task_type=t, task_id=i, task_name=n,
                        level=lv, message=m, detail=d,
                    )
                    for (t, i, n, lv, m, d) in rows
                ]
            )
            db.commit()
            return len(rows)
        except Exception as exc:
            logger.error(f"批量写入运行日志失败: {exc}")
            return 0
        finally:
            db.close()


run_log_buffer = _RunLogBuffer()
atexit.register(run_log_buffer.stop)


def task_log(
    task_type: str,
    task_id: int,
    task_name: Optional[str],
    level: str,
    message: str,
    detail: Optional[str] = None,
) -> None:
    """高吞吐路径使用的运行日志写入（进缓冲）。"""
    run_log_buffer.enqueue(task_type, task_id, task_name, level, message, detail)
