"""
数据库模型定义。

枚举一律以「值」形式落库（values_callable），便于直接阅读原始表数据。
"""
import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

from ..core.config import now_beijing

Base = declarative_base()


def _enum_values(enum_cls):
    """让 SQLAlchemy 以枚举的 value 而非 name 落库。"""
    return [member.value for member in enum_cls]


def _enum(enum_cls, **kwargs):
    return SAEnum(
        enum_cls,
        values_callable=_enum_values,
        native_enum=False,
        validate_strings=True,
        **kwargs,
    )


# ============================================================ 枚举


class DatabaseType(enum.Enum):
    MYSQL = "mysql"


class SyncStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    STOPPED = "stopped"


class SyncHealth(enum.Enum):
    """同步任务健康度。running 不等于健康：此前版本用单一 running 状态掩盖了丢事件。"""
    HEALTHY = "healthy"      # 位点正常推进
    DEGRADED = "degraded"    # 有事件进 DLQ，但仍在推进
    STALLED = "stalled"      # 位点长时间未推进
    UNKNOWN = "unknown"


class BackupType(enum.Enum):
    """仅保留全量备份。增量备份功能已移除。"""
    FULL = "full"


class BackupStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class UploadStatus(enum.Enum):
    """备份文件上传到 OpenList 的状态。"""
    SKIPPED = "skipped"      # 未开启上传
    PENDING = "pending"      # 待上传
    UPLOADING = "uploading"
    SUCCESS = "success"
    FAILED = "failed"


class TableSyncState(enum.Enum):
    """单表在某个同步任务下的状态。"""
    ACTIVE = "active"
    SCHEMA_MISSING = "schema_missing"   # 目标库缺表，等待 DDL 事件重建
    SKIPPED = "skipped"                 # 人工跳过
    ERRORED = "errored"                 # 持续失败，已进 DLQ
    NO_PRIMARY_KEY = "no_primary_key"   # 仍在同步，但定位键不可靠，UPDATE/DELETE 有风险


# ============================================================ 控制面

class Database(Base):
    """MySQL 数据库连接配置。密码以 Fernet 密文存储。"""
    __tablename__ = "databases"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True, comment="显示名称")
    host = Column(String(255), nullable=False)
    port = Column(Integer, default=3306, nullable=False)
    username = Column(String(100), nullable=False)
    password = Column(String(255), nullable=False, comment="Fernet 密文")
    database_name = Column(String(100), nullable=False)
    db_type = Column(_enum(DatabaseType), default=DatabaseType.MYSQL, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=now_beijing, nullable=False)
    updated_at = Column(DateTime, default=now_beijing, onupdate=now_beijing, nullable=False)

    def __repr__(self) -> str:
        return f"<Database {self.name}>"


class SyncTask(Base):
    """源库 → 目标库的 binlog 实时同步任务。"""
    __tablename__ = "sync_tasks"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True)
    source_db_id = Column(Integer, ForeignKey("databases.id"), nullable=False)
    target_db_id = Column(Integer, ForeignKey("databases.id"), nullable=False)

    status = Column(_enum(SyncStatus), default=SyncStatus.PENDING, nullable=False)
    health = Column(_enum(SyncHealth), default=SyncHealth.UNKNOWN, nullable=False)
    error_message = Column(Text)
    auto_start = Column(Boolean, default=False, nullable=False, comment="进程重启后是否自动恢复")

    # ---- 位点。GTID 优先于文件位点，不受 binlog 轮转/PURGE 影响。
    gtid_set = Column(String(512), comment="已应用的 GTID 集合")
    binlog_file = Column(String(255))
    binlog_position = Column(BigInteger)

    # ---- 固定 server_id，避免每次随机的重放窗口不可预测
    server_id = Column(Integer, comment="binlog 复制 server_id")

    # ---- 运行指标
    last_sync_time = Column(DateTime)
    sync_delay = Column(Integer, default=0, comment="端到端延迟(毫秒)")
    applied_events = Column(BigInteger, default=0, nullable=False, comment="累计已应用事件数")
    dlq_count = Column(Integer, default=0, nullable=False, comment="失败事件计数")

    # ---- binlog 兼容性快照，启动时校验
    binlog_format = Column(String(20), comment="源库 binlog_format，必须为 ROW")
    binlog_row_image = Column(String(20), comment="源库 binlog_row_image，必须为 FULL")

    created_at = Column(DateTime, default=now_beijing, nullable=False)
    updated_at = Column(DateTime, default=now_beijing, onupdate=now_beijing, nullable=False)

    source_db = relationship("Database", foreign_keys=[source_db_id])
    target_db = relationship("Database", foreign_keys=[target_db_id])
    table_states = relationship(
        "SyncTableState", back_populates="task", cascade="all, delete-orphan"
    )
    errors = relationship("SyncError", back_populates="task", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<SyncTask {self.name} {self.status.value}>"


class SyncTableState(Base):
    """
    每个同步任务下每张表的独立状态与位点。

    取代此前「整表拉黑后静默跳过」的黑盒行为：表为什么没在同步、卡在哪里，
    现在都能直接查出来。
    """
    __tablename__ = "sync_table_state"
    __table_args__ = (
        UniqueConstraint("task_id", "schema_name", "table_name", name="uq_sync_table"),
        Index("ix_sync_table_task", "task_id"),
    )

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("sync_tasks.id", ondelete="CASCADE"), nullable=False)
    schema_name = Column(String(100), nullable=False)
    table_name = Column(String(100), nullable=False)

    state = Column(_enum(TableSyncState), default=TableSyncState.ACTIVE, nullable=False)
    pk_columns = Column(JSON, comment="主键列名列表，用于构造 WHERE")
    unique_keys = Column(JSON, comment="唯一键列表（无主键时的退化方案）")

    last_binlog_file = Column(String(255))
    last_binlog_pos = Column(BigInteger)
    last_applied_at = Column(DateTime)
    applied_events = Column(BigInteger, default=0, nullable=False)
    error_count = Column(Integer, default=0, nullable=False)
    last_error = Column(Text)

    created_at = Column(DateTime, default=now_beijing, nullable=False)
    updated_at = Column(DateTime, default=now_beijing, onupdate=now_beijing, nullable=False)

    task = relationship("SyncTask", back_populates="table_states")

    def __repr__(self) -> str:
        return f"<SyncTableState {self.schema_name}.{self.table_name} {self.state.value}>"


class SyncError(Base):
    """
    失败事件队列（DLQ）。

    此前版本遇到错误要么静默跳过、要么整表拉黑；现在失败事件连同行数据一并留存，
    可在前端查看并重放。
    """
    __tablename__ = "sync_errors"
    __table_args__ = (Index("ix_sync_error_task_created", "task_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("sync_tasks.id", ondelete="CASCADE"), nullable=False)

    schema_name = Column(String(100), nullable=False)
    table_name = Column(String(100), nullable=False)
    event_type = Column(String(20), nullable=False, comment="insert|update|delete|ddl")

    error_code = Column(Integer, comment="MySQL 错误码")
    error_message = Column(Text, nullable=False)
    payload = Column(Text, comment="事件内容 JSON，用于重放")

    binlog_file = Column(String(255))
    binlog_pos = Column(BigInteger)

    retry_count = Column(Integer, default=0, nullable=False)
    resolved = Column(Boolean, default=False, nullable=False, comment="是否已重放成功")
    created_at = Column(DateTime, default=now_beijing, nullable=False)

    task = relationship("SyncTask", back_populates="errors")

    def __repr__(self) -> str:
        return f"<SyncError {self.schema_name}.{self.table_name} {self.error_message[:30]}>"


class BackupPlan(Base):
    """全量备份计划。"""
    __tablename__ = "backup_plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True)
    database_id = Column(Integer, ForeignKey("databases.id"), nullable=False)
    backup_type = Column(_enum(BackupType), default=BackupType.FULL, nullable=False)
    schedule_interval = Column(Integer, nullable=False, comment="间隔(分钟)")
    retention_count = Column(Integer, default=50, nullable=False, comment="保留最近N份")
    is_active = Column(Boolean, default=True, nullable=False)

    last_run_at = Column(DateTime)
    next_run_at = Column(DateTime)

    # 备份完成后上传到 OpenList。
    # 远程目录留空则使用全局配置中的目录，便于「同一个网盘、按计划分目录」。
    upload_enabled = Column(Boolean, default=False, nullable=False, comment="备份后是否上传")
    upload_dir = Column(String(500), comment="远程目录；留空用全局配置")

    created_at = Column(DateTime, default=now_beijing, nullable=False)
    updated_at = Column(DateTime, default=now_beijing, onupdate=now_beijing, nullable=False)

    database = relationship("Database")
    history = relationship(
        "BackupHistory", back_populates="plan", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<BackupPlan {self.name}>"


class BackupHistory(Base):
    """备份执行历史。"""
    __tablename__ = "backup_history"
    __table_args__ = (Index("ix_backup_history_plan_created", "backup_plan_id", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)
    backup_plan_id = Column(Integer, ForeignKey("backup_plans.id", ondelete="CASCADE"), nullable=False)

    status = Column(_enum(BackupStatus), default=BackupStatus.PENDING, nullable=False)
    backup_type = Column(_enum(BackupType), nullable=False)

    file_path = Column(String(500))
    file_size = Column(BigInteger)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    duration = Column(Integer, comment="耗时(秒)")
    error_message = Column(Text)
    trigger = Column(String(20), default="schedule", comment="schedule|manual")

    # 上传到 OpenList 的结果。上传失败不影响备份本身的成功状态，
    # 因此单独记录，界面上也能看出「备份成功但没传上去」。
    upload_status = Column(
        _enum(UploadStatus), default=UploadStatus.SKIPPED, nullable=False, comment="上传状态"
    )
    upload_path = Column(String(1000), comment="远程文件路径")
    upload_error = Column(Text, comment="上传失败原因")
    uploaded_at = Column(DateTime, comment="上传完成时间")

    created_at = Column(DateTime, default=now_beijing, nullable=False)

    plan = relationship("BackupPlan", back_populates="history")

    def __repr__(self) -> str:
        return f"<BackupHistory {self.id} {self.status.value}>"


# ============================================================ 系统与审计

class SystemConfig(Base):
    """键值配置。"""
    __tablename__ = "system_config"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), nullable=False, unique=True)
    value = Column(Text)
    description = Column(String(255))
    created_at = Column(DateTime, default=now_beijing, nullable=False)
    updated_at = Column(DateTime, default=now_beijing, onupdate=now_beijing, nullable=False)

    def __repr__(self) -> str:
        return f"<SystemConfig {self.key}>"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    token_version = Column(Integer, default=0, nullable=False, comment="递增使旧令牌失效")
    created_at = Column(DateTime, default=now_beijing, nullable=False)
    last_login = Column(DateTime)

    def __repr__(self) -> str:
        return f"<User {self.username}>"


class OperationLog(Base):
    __tablename__ = "operation_logs"
    __table_args__ = (Index("ix_operation_log_created", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    username = Column(String(50))
    action = Column(String(50), nullable=False)
    resource_type = Column(String(50))
    resource_id = Column(Integer)
    resource_name = Column(String(100))
    detail = Column(Text)
    ip_address = Column(String(64))
    created_at = Column(DateTime, default=now_beijing, nullable=False)

    def __repr__(self) -> str:
        return f"<OperationLog {self.action}>"


class LoginLog(Base):
    __tablename__ = "login_logs"
    __table_args__ = (Index("ix_login_log_created", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), nullable=False)
    ip_address = Column(String(64))
    user_agent = Column(String(500))
    success = Column(Boolean, default=True, nullable=False)
    failure_reason = Column(String(200))
    created_at = Column(DateTime, default=now_beijing, nullable=False)

    def __repr__(self) -> str:
        return f"<LoginLog {self.username}>"


class RunLog(Base):
    __tablename__ = "run_logs"
    __table_args__ = (Index("ix_run_log_task_created", "task_type", "task_id", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)
    task_type = Column(String(20), nullable=False, comment="sync|backup|restore")
    task_id = Column(Integer, nullable=False)
    task_name = Column(String(100))
    level = Column(String(20), default="INFO", nullable=False)
    message = Column(Text, nullable=False)
    detail = Column(Text)
    created_at = Column(DateTime, default=now_beijing, nullable=False)

    def __repr__(self) -> str:
        return f"<RunLog {self.task_type}:{self.task_id}>"
