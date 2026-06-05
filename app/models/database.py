"""
数据库模型定义
"""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Enum, ForeignKey, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

from ..core.config import now_beijing

Base = declarative_base()


class DatabaseType(enum.Enum):
    """数据库类型枚举"""
    MYSQL = "mysql"


class SyncStatus(enum.Enum):
    """同步状态枚举"""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    STOPPED = "stopped"


class BackupType(enum.Enum):
    """备份类型枚举"""
    FULL = "full"
    INCREMENTAL = "incremental"


class BackupStatus(enum.Enum):
    """备份状态枚举"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Database(Base):
    """数据库连接配置表"""
    __tablename__ = "databases"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True, comment="数据库名称")
    host = Column(String(255), nullable=False, comment="主机地址")
    port = Column(Integer, default=3306, comment="端口")
    username = Column(String(100), nullable=False, comment="用户名")
    password = Column(String(255), nullable=False, comment="密码")
    database_name = Column(String(100), nullable=False, comment="数据库名")
    db_type = Column(Enum(DatabaseType), default=DatabaseType.MYSQL, comment="数据库类型")
    is_active = Column(Boolean, default=True, comment="是否启用")
    created_at = Column(DateTime, default=now_beijing, comment="创建时间")
    updated_at = Column(DateTime, default=now_beijing, onupdate=now_beijing, comment="更新时间")
    
    def __repr__(self):
        return f"<Database {self.name}>"


class SyncTask(Base):
    """同步任务配置表"""
    __tablename__ = "sync_tasks"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, comment="任务名称")
    source_db_id = Column(Integer, ForeignKey("databases.id"), nullable=False, comment="源数据库ID")
    target_db_id = Column(Integer, ForeignKey("databases.id"), nullable=False, comment="目标数据库ID")
    status = Column(Enum(SyncStatus), default=SyncStatus.PENDING, comment="同步状态")
    last_sync_time = Column(DateTime, comment="最后同步时间")
    sync_delay = Column(Integer, default=0, comment="同步延迟(毫秒)")
    error_message = Column(Text, comment="错误信息")
    binlog_position = Column(String(50), comment="binlog位置")
    binlog_file = Column(String(100), comment="binlog文件名")
    created_at = Column(DateTime, default=now_beijing, comment="创建时间")
    updated_at = Column(DateTime, default=now_beijing, onupdate=now_beijing, comment="更新时间")
    
    def __repr__(self):
        return f"<SyncTask {self.name}>"


class BackupPlan(Base):
    """备份计划配置表"""
    __tablename__ = "backup_plans"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, comment="计划名称")
    database_id = Column(Integer, ForeignKey("databases.id"), nullable=False, comment="数据库ID")
    backup_type = Column(Enum(BackupType), default=BackupType.FULL, comment="备份类型")
    schedule_cron = Column(String(100), comment="Cron表达式")
    schedule_interval = Column(Integer, comment="间隔(分钟)")
    backup_path = Column(String(500), comment="备份路径")
    retention_days = Column(Integer, default=30, comment="保留天数")
    is_active = Column(Boolean, default=True, comment="是否启用")
    created_at = Column(DateTime, default=now_beijing, comment="创建时间")
    updated_at = Column(DateTime, default=now_beijing, onupdate=now_beijing, comment="更新时间")
    
    # 关系
    backup_history = relationship("BackupHistory", back_populates="backup_plan")
    
    def __repr__(self):
        return f"<BackupPlan {self.name}>"


class BackupHistory(Base):
    """备份历史记录表"""
    __tablename__ = "backup_history"
    
    id = Column(Integer, primary_key=True, index=True)
    backup_plan_id = Column(Integer, ForeignKey("backup_plans.id"), nullable=False, comment="备份计划ID")
    status = Column(Enum(BackupStatus), default=BackupStatus.PENDING, comment="备份状态")
    backup_type = Column(Enum(BackupType), nullable=False, comment="备份类型")
    file_path = Column(String(500), comment="备份文件路径")
    file_size = Column(Integer, comment="文件大小(字节)")
    start_time = Column(DateTime, comment="开始时间")
    end_time = Column(DateTime, comment="结束时间")
    duration = Column(Integer, comment="耗时(秒)")
    error_message = Column(Text, comment="错误信息")
    created_at = Column(DateTime, default=now_beijing, comment="创建时间")
    
    # 关系
    backup_plan = relationship("BackupPlan", back_populates="backup_history")
    
    def __repr__(self):
        return f"<BackupHistory {self.id}>"


class SystemConfig(Base):
    """系统配置表"""
    __tablename__ = "system_config"
    
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), nullable=False, unique=True, comment="配置键")
    value = Column(Text, comment="配置值")
    description = Column(String(255), comment="配置描述")
    created_at = Column(DateTime, default=now_beijing, comment="创建时间")
    updated_at = Column(DateTime, default=now_beijing, onupdate=now_beijing, comment="更新时间")
    
    def __repr__(self):
        return f"<SystemConfig {self.key}>"


class User(Base):
    """用户表"""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), nullable=False, unique=True, comment="用户名")
    password_hash = Column(String(255), nullable=False, comment="密码哈希")
    is_active = Column(Boolean, default=True, comment="是否启用")
    created_at = Column(DateTime, default=now_beijing, comment="创建时间")
    last_login = Column(DateTime, comment="最后登录时间")
    
    def __repr__(self):
        return f"<User {self.username}>"


class OperationLog(Base):
    """操作日志表"""
    __tablename__ = "operation_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), comment="操作用户ID")
    username = Column(String(50), comment="用户名")
    action = Column(String(50), nullable=False, comment="操作类型")
    resource_type = Column(String(50), comment="资源类型")
    resource_id = Column(Integer, comment="资源ID")
    resource_name = Column(String(100), comment="资源名称")
    detail = Column(Text, comment="操作详情")
    ip_address = Column(String(50), comment="IP地址")
    created_at = Column(DateTime, default=now_beijing, comment="创建时间")
    
    def __repr__(self):
        return f"<OperationLog {self.action}>"


class LoginLog(Base):
    """登录日志表"""
    __tablename__ = "login_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), nullable=False, comment="用户名")
    ip_address = Column(String(50), comment="IP地址")
    user_agent = Column(String(500), comment="User-Agent")
    success = Column(Boolean, default=True, comment="是否成功")
    failure_reason = Column(String(200), comment="失败原因")
    created_at = Column(DateTime, default=now_beijing, comment="创建时间")
    
    def __repr__(self):
        return f"<LoginLog {self.username}>"


class RunLog(Base):
    """运行日志表"""
    __tablename__ = "run_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    task_type = Column(String(20), nullable=False, comment="任务类型(sync/backup)")
    task_id = Column(Integer, nullable=False, comment="任务ID")
    task_name = Column(String(100), comment="任务名称")
    level = Column(String(20), default="INFO", comment="日志级别")
    message = Column(Text, nullable=False, comment="日志消息")
    detail = Column(Text, comment="详细信息")
    created_at = Column(DateTime, default=now_beijing, comment="创建时间")
    
    def __repr__(self):
        return f"<RunLog {self.task_type}:{self.task_id}>"