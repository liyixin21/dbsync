"""
控制面数据库结构迁移。

为什么不引入 Alembic：本项目的结构演进是低频、小步的（加列、改枚举存储格式），
一个显式的幂等迁移清单比完整的迁移框架更易读、更易审计。
这也避免了「依赖清单里声明了一个从没被使用的库」这类问题。

每个迁移步骤都必须是幂等的：可以重复执行而结果不变。
"""
import enum
import json
from typing import Callable, List, Tuple

from loguru import logger
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def _columns(engine: Engine, table: str) -> set:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def _tables(engine: Engine) -> set:
    return set(inspect(engine).get_table_names())


def _add_column(engine: Engine, table: str, column: str, ddl: str) -> bool:
    """幂等地为表添加列。返回是否实际执行了变更。"""
    if table not in _tables(engine):
        return False
    if column in _columns(engine, table):
        return False
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    logger.info(f"[迁移] 已添加 {table}.{column}")
    return True


# ============================================================ 迁移步骤

def _m_001_users_token_version(engine: Engine) -> None:
    """令牌版本号，用于改密/改名后主动失效旧令牌。"""
    _add_column(engine, "users", "token_version", "INTEGER DEFAULT 0 NOT NULL")


def _m_002_backup_plans_retention_count(engine: Engine) -> None:
    """保留份数（旧版本用的是 retention_days）。"""
    _add_column(engine, "backup_plans", "retention_count", "INTEGER DEFAULT 50 NOT NULL")


def _m_003_backup_plans_new_columns(engine: Engine) -> None:
    _add_column(engine, "backup_plans", "last_run_at", "DATETIME")
    _add_column(engine, "backup_plans", "next_run_at", "DATETIME")


def _m_004_backup_history_trigger(engine: Engine) -> None:
    """区分手动执行与定时执行。"""
    _add_column(engine, "backup_history", "trigger", "VARCHAR(20) DEFAULT 'schedule'")


def _m_005_sync_tasks_new_columns(engine: Engine) -> None:
    """同步任务的可观测性与位点字段。"""
    _add_column(engine, "sync_tasks", "health", "VARCHAR(20) DEFAULT 'unknown' NOT NULL")
    _add_column(engine, "sync_tasks", "auto_start", "BOOLEAN DEFAULT 0 NOT NULL")
    _add_column(engine, "sync_tasks", "gtid_set", "VARCHAR(512)")
    _add_column(engine, "sync_tasks", "server_id", "INTEGER")
    _add_column(engine, "sync_tasks", "applied_events", "BIGINT DEFAULT 0 NOT NULL")
    _add_column(engine, "sync_tasks", "dlq_count", "INTEGER DEFAULT 0 NOT NULL")
    _add_column(engine, "sync_tasks", "binlog_format", "VARCHAR(20)")
    _add_column(engine, "sync_tasks", "binlog_row_image", "VARCHAR(20)")


def _m_006_normalise_enum_storage(engine: Engine) -> None:
    """
    把枚举列从「成员名」统一为「成员值」。

    SQLAlchemy 默认把 Enum 的 **name** 落库（如 RUNNING），
    而 API 与前端使用 value（如 running）。旧版本因此存在
    数据库内容与接口响应不一致的问题。
    """
    mappings = [
        ("sync_tasks", "status", {
            "PENDING": "pending", "RUNNING": "running", "PAUSED": "paused",
            "FAILED": "failed", "STOPPED": "stopped",
        }),
        ("sync_tasks", "health", {
            "HEALTHY": "healthy", "DEGRADED": "degraded",
            "STALLED": "stalled", "UNKNOWN": "unknown",
        }),
        ("backup_plans", "backup_type", {"FULL": "full", "INCREMENTAL": "full"}),
        ("backup_history", "backup_type", {"FULL": "full", "INCREMENTAL": "full"}),
        ("backup_history", "status", {
            "PENDING": "pending", "RUNNING": "running",
            "COMPLETED": "completed", "FAILED": "failed",
        }),
        ("databases", "db_type", {"MYSQL": "mysql"}),
    ]

    with engine.begin() as conn:
        for table, column, mapping in mappings:
            if table not in _tables(engine):
                continue
            if column not in _columns(engine, table):
                continue
            for old, new in mapping.items():
                if old == new:
                    continue
                result = conn.execute(
                    text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"),
                    {"new": new, "old": old},
                )
                if result.rowcount:
                    logger.info(
                        f"[迁移] {table}.{column}: {old} → {new}（{result.rowcount} 行）"
                    )


def _m_007_drop_incremental_plans(engine: Engine) -> None:
    """
    增量备份功能已移除。

    把类型为 incremental 的计划转为全量，历史记录则删除
    （增量 binlog 导出文件对新的全量恢复流程没有意义）。
    """
    if "backup_history" not in _tables(engine):
        return
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM backup_history WHERE backup_type = 'incremental'")
        )
        if result.rowcount:
            logger.warning(
                f"[迁移] 已删除 {result.rowcount} 条增量备份历史记录"
                "（增量备份功能已移除）"
            )


def _m_008_migrate_theme_config(engine: Engine) -> None:
    """清理已废弃的 MD3 主题配置键。"""
    if "system_config" not in _tables(engine):
        return
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "DELETE FROM system_config "
                "WHERE key IN ('theme_scheme', 'contrast_level')"
            )
        )
        if result.rowcount:
            logger.info(f"[迁移] 已清理 {result.rowcount} 条废弃的 MD3 主题配置")

        # 旧的默认主题色是 MD3 的紫罗兰，换成新方案的蓝
        conn.execute(
            text(
                "UPDATE system_config SET value = :new "
                "WHERE key = 'primary_color' AND value = :old"
            ),
            {"new": "#2f6feb", "old": "#6750A4"},
        )


def _m_009_openlist_upload(engine: Engine) -> None:
    """备份文件上传到 OpenList：计划级开关与历史级上传状态。"""
    _add_column(engine, "backup_plans", "upload_enabled", "BOOLEAN DEFAULT 0 NOT NULL")
    _add_column(engine, "backup_plans", "upload_dir", "VARCHAR(500)")

    _add_column(engine, "backup_history", "upload_status", "VARCHAR(20) DEFAULT 'skipped' NOT NULL")
    _add_column(engine, "backup_history", "upload_path", "VARCHAR(1000)")
    _add_column(engine, "backup_history", "upload_error", "TEXT")
    _add_column(engine, "backup_history", "uploaded_at", "DATETIME")


MIGRATIONS: List[Tuple[str, str, Callable[[Engine], None]]] = [
    ("001", "users.token_version", _m_001_users_token_version),
    ("002", "backup_plans.retention_count", _m_002_backup_plans_retention_count),
    ("003", "backup_plans 调度时间字段", _m_003_backup_plans_new_columns),
    ("004", "backup_history.trigger", _m_004_backup_history_trigger),
    ("005", "sync_tasks 可观测性字段", _m_005_sync_tasks_new_columns),
    ("006", "统一枚举落库格式", _m_006_normalise_enum_storage),
    ("007", "移除增量备份计划", _m_007_drop_incremental_plans),
    ("008", "迁移主题配置", _m_008_migrate_theme_config),
    ("009", "OpenList 上传配置", _m_009_openlist_upload),
]


def run_migrations(engine: Engine) -> int:
    """
    执行全部迁移。

    返回失败步骤数。单个步骤失败不会中断其余步骤——
    迁移失败应当在启动日志里明确可见，而不是让服务直接起不来。
    """
    failures = 0
    for code, description, fn in MIGRATIONS:
        try:
            fn(engine)
        except Exception as exc:
            failures += 1
            logger.error(f"[迁移] 步骤 {code}（{description}）失败: {exc}")
    return failures
