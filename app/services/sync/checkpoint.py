"""
位点持久化。

旧实现有两条致命路径：位点每 100 条事件才落盘一次，且异常退出时根本不落盘。
结果是崩溃后重放窗口不可预测，重放又因非幂等写入而制造主键冲突。

这里的策略：

- **每事务提交后落盘**，落盘频率由时间节流（默认 2 秒）而非事件计数控制。
- **GTID 优先**：GTID 不受 binlog 轮转与 PURGE 影响，比文件位点健壮。
- **停机强制落盘**：无论正常停止还是异常退出，都把最后已提交位点写下去。
"""
import threading
import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from ...core.database import session_scope
from ...core.config import now_beijing
from ...models.database import SyncStatus, SyncTask


@dataclass
class Checkpoint:
    """一个位点快照。"""

    gtid_set: Optional[str] = None
    binlog_file: Optional[str] = None
    binlog_pos: Optional[int] = None

    def is_valid(self) -> bool:
        return bool(self.gtid_set) or bool(self.binlog_file and self.binlog_pos)


@dataclass
class RuntimeMetrics:
    """运行指标。"""

    applied_events: int = 0
    dlq_count: int = 0
    last_event_ts: Optional[float] = None
    last_progress_at: float = 0.0


class CheckpointStore:
    """
    位点与运行指标的持久化。

    写库操作可能阻塞（SQLite 文件锁），因此所有落盘都在独立线程完成，
    绝不阻塞 binlog 消费主循环。
    """

    def __init__(
        self,
        task_id: int,
        *,
        interval: float = 2.0,
        status: SyncStatus = SyncStatus.RUNNING,
    ) -> None:
        self.task_id = task_id
        self.interval = interval
        self._checkpoint = Checkpoint()
        self._metrics = RuntimeMetrics(last_progress_at=time.monotonic())
        self._status = status
        self._health: Optional[str] = None
        self._error: Optional[str] = None
        self._delay_ms: int = 0
        self._binlog_format: Optional[str] = None
        self._binlog_row_image: Optional[str] = None

        self._lock = threading.RLock()
        self._last_flush = 0.0
        self._dirty = False
        self._closed = False

    # ------------------------------------------------------------ 更新

    def update_position(
        self,
        *,
        gtid_set: Optional[str] = None,
        binlog_file: Optional[str] = None,
        binlog_pos: Optional[int] = None,
    ) -> None:
        """记录新位点。仅当事务已提交后调用。"""
        with self._lock:
            if gtid_set:
                self._checkpoint.gtid_set = gtid_set
            if binlog_file:
                self._checkpoint.binlog_file = binlog_file
            if binlog_pos is not None:
                self._checkpoint.binlog_pos = binlog_pos
            self._dirty = True
            self._metrics.last_progress_at = time.monotonic()

    def record_events(self, count: int, event_timestamp: Optional[float] = None) -> None:
        """累计已应用事件数与延迟。"""
        with self._lock:
            self._metrics.applied_events += max(0, count)
            if event_timestamp:
                self._metrics.last_event_ts = event_timestamp
                self._delay_ms = max(0, int((time.time() - event_timestamp) * 1000))

    def record_dlq(self, count: int = 1) -> None:
        with self._lock:
            self._metrics.dlq_count += count

    def set_status(self, status: SyncStatus, error: Optional[str] = None) -> None:
        with self._lock:
            self._status = status
            self._error = error
            self._dirty = True

    def set_health(self, health: str) -> None:
        with self._lock:
            self._health = health
            self._dirty = True

    def set_binlog_capabilities(self, fmt: Optional[str], row_image: Optional[str]) -> None:
        with self._lock:
            self._binlog_format = fmt
            self._binlog_row_image = row_image
            self._dirty = True

    # ------------------------------------------------------------ 读取

    @property
    def position(self) -> Checkpoint:
        with self._lock:
            return Checkpoint(
                gtid_set=self._checkpoint.gtid_set,
                binlog_file=self._checkpoint.binlog_file,
                binlog_pos=self._checkpoint.binlog_pos,
            )

    @property
    def metrics(self) -> RuntimeMetrics:
        with self._lock:
            return RuntimeMetrics(
                applied_events=self._metrics.applied_events,
                dlq_count=self._metrics.dlq_count,
                last_event_ts=self._metrics.last_event_ts,
                last_progress_at=self._metrics.last_progress_at,
            )

    @property
    def seconds_since_progress(self) -> float:
        with self._lock:
            return time.monotonic() - self._metrics.last_progress_at

    # ------------------------------------------------------------ 落盘

    def maybe_flush(self, force: bool = False) -> None:
        """按时间节流落盘。"""
        if self._closed and not force:
            return
        now = time.monotonic()
        with self._lock:
            if not force and (not self._dirty or now - self._last_flush < self.interval):
                return
            self._last_flush = now
            self._dirty = False
            snapshot = self._snapshot_locked()

        self._write(snapshot)

    def flush(self) -> None:
        """强制落盘。停机路径必须调用。"""
        self.maybe_flush(force=True)

    def _snapshot_locked(self) -> dict:
        return {
            "gtid_set": self._checkpoint.gtid_set,
            "binlog_file": self._checkpoint.binlog_file,
            "binlog_position": self._checkpoint.binlog_pos,
            "status": self._status,
            "health": self._health,
            "error_message": self._error,
            "applied_events": self._metrics.applied_events,
            "dlq_count": self._metrics.dlq_count,
            "sync_delay": self._delay_ms,
            "binlog_format": self._binlog_format,
            "binlog_row_image": self._binlog_row_image,
        }

    def _write(self, snapshot: dict) -> None:
        """写库。异常不外抛——位点落盘失败不应打断同步。"""
        try:
            with session_scope() as db:
                task = db.query(SyncTask).filter(SyncTask.id == self.task_id).first()
                if task is None:
                    return
                task.gtid_set = snapshot["gtid_set"]
                if snapshot["binlog_file"]:
                    task.binlog_file = snapshot["binlog_file"]
                if snapshot["binlog_position"] is not None:
                    task.binlog_position = snapshot["binlog_position"]
                task.status = snapshot["status"]
                if snapshot["health"]:
                    task.health = snapshot["health"]
                task.error_message = snapshot["error_message"]
                task.applied_events = snapshot["applied_events"]
                task.dlq_count = snapshot["dlq_count"]
                task.sync_delay = snapshot["sync_delay"]
                if snapshot["binlog_format"] is not None:
                    task.binlog_format = snapshot["binlog_format"]
                if snapshot["binlog_row_image"] is not None:
                    task.binlog_row_image = snapshot["binlog_row_image"]
                if snapshot["status"] == SyncStatus.RUNNING:
                    task.last_sync_time = now_beijing()
        except Exception as exc:
            logger.error(f"任务 {self.task_id} 位点落盘失败: {exc}")

    def close(self) -> None:
        """停机：强制落盘并标记关闭。"""
        self.flush()
        with self._lock:
            self._closed = True


def load_checkpoint(task: SyncTask) -> Checkpoint:
    """从任务记录中恢复位点。"""
    return Checkpoint(
        gtid_set=task.gtid_set,
        binlog_file=task.binlog_file,
        binlog_pos=task.binlog_position,
    )
