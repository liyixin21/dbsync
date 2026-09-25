"""
应用配置。

所有配置项均可通过环境变量或 .env 文件覆盖。
启动期执行强校验：生产模式（DEBUG=false）下使用默认 SECRET_KEY 将拒绝启动。
"""
import sys
from datetime import datetime, timedelta, timezone
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

# 北京时区 (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))

# 出现即视为未配置的占位密钥
DEFAULT_SECRET = "change-me-in-production-env"
INSECURE_SECRETS = frozenset({
    DEFAULT_SECRET,
    "change-this-to-a-random-secret-key",
    "secret",
    "changeme",
})


def now_beijing() -> datetime:
    """返回北京时间 naive datetime（全库时间统一为北京时间）。"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


class Settings(BaseSettings):
    """应用配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 应用
    APP_NAME: str = "数据库同步备份工具"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False

    # 服务器
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # 控制面数据库（SQLite）
    DATABASE_URL: str = "sqlite:///./data/dbsync.db"

    # 备份
    BACKUP_DIR: str = "./backups"
    MAX_BACKUP_FILES: int = 100
    BACKUP_TIMEOUT: int = 3600

    # 日志
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "./data/dbsync.log"

    # 安全
    SECRET_KEY: str = DEFAULT_SECRET
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    LOGIN_MAX_ATTEMPTS: int = 10
    LOGIN_WINDOW_SECONDS: int = 300
    CORS_ORIGINS: str = ""

    # 同步引擎
    SYNC_MAX_TASKS: int = 8
    SYNC_TX_MAX_ROWS: int = 10000          # 单事务缓冲上限，防止大事务 OOM
    SYNC_CHECKPOINT_INTERVAL: float = 2.0  # 位点落盘间隔（秒）
    SYNC_RETRY_BASE_DELAY: float = 2.0
    SYNC_RETRY_MAX_DELAY: float = 60.0
    SYNC_SERVER_ID_BASE: int = 10000
    SYNC_SERVER_ID_RANGE: int = 50000
    DLQ_MAX_ROWS: int = 100000             # 失败事件队列上限

    @property
    def cors_origins(self) -> List[str]:
        """解析逗号分隔的跨域白名单。为空表示不启用 CORS 中间件。"""
        return [o.strip() for o in (self.CORS_ORIGINS or "").split(",") if o.strip()]

    @property
    def is_insecure_secret(self) -> bool:
        key = (self.SECRET_KEY or "").strip()
        return key in INSECURE_SECRETS or len(key) < 16

    def validate_for_startup(self) -> None:
        """启动期校验。生产模式下配置不安全直接拒绝启动。"""
        if not self.is_insecure_secret:
            return
        hint = (
            "请在 .env 或环境变量中设置 SECRET_KEY。"
            '生成方式: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
        if self.DEBUG:
            print(f"[WARNING] SECRET_KEY 未设置或强度不足，{hint}", file=sys.stderr)
            return
        raise RuntimeError(
            f"配置校验失败：SECRET_KEY 未设置或强度不足。{hint}\n"
            "（如需以开发模式启动，请设置 DEBUG=true）"
        )


settings = Settings()
