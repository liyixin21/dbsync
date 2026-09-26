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
    # binlog 心跳间隔（秒）。源库对空闲从库连接会按 wait_timeout 掐断，
    # 默认 8.0 为 8 小时、云数据库常配 30~60 秒，一旦掐断就会走库的重连路径。
    # 保持心跳可避免这种无谓断连。必须小于源库 wait_timeout 的一半。
    SYNC_HEARTBEAT_SECONDS: float = 15.0
    # 同步线程意外死亡后的自动重启上限（次）与退避基准（秒）。
    # 设为 0 可关闭自愈。库缺陷、连接抖动等都可能打死线程，
    # 没有自愈就只能靠人工发现——夜间无人值守时等于数据停更。
    # 重试始终从最后已提交位点开始，且写入是幂等的，重复应用无副作用；
    # 上限用尽后任务置为 failed，不会无限重启。
    SYNC_AUTO_RECOVER_MAX: int = 5
    SYNC_AUTO_RECOVER_DELAY: float = 10.0
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
        """
        启动期校验：生产模式下 SECRET_KEY 不安全则拒绝启动。

        刻意采用「拒绝启动」而不是「仅告警」：
        SECRET_KEY 用于加密数据库密码与 OpenList 密码，
        沿用公开的占位值等于把凭据以众所周知的密钥加密，而这类问题
        一旦只在日志里告警就基本没人会注意到。

        提示会先于 traceback 打印，避免用户被栈信息挡住真正的原因。
        """
        if not self.is_insecure_secret:
            return

        generate_cmd = 'python -c "import secrets; print(secrets.token_urlsafe(32))"'
        hint = f"生成方式：{generate_cmd}"

        if self.DEBUG:
            print(f"[WARNING] SECRET_KEY 未设置或强度不足。{hint}", file=sys.stderr)
            return

        # 先打印可读提示，再抛异常——用户第一眼看到的就是解决办法
        banner = (
            "\n"
            + "=" * 68
            + "\n  启动失败：SECRET_KEY 未设置或强度不足\n"
            + "=" * 68
            + "\n\n"
            + "  这个密钥用于加密数据库密码与 OpenList 密码，\n"
            + "  使用默认占位值会让这些凭据形同明文存储。\n"
            + "\n"
            + "  解决办法（任选其一）：\n"
            + "\n"
            + f"    1) 生成一个随机密钥：\n        {generate_cmd}\n"
            + "\n"
            + "    2) 在 docker-compose.yml 中把该密钥填到：\n"
            + "         - SECRET_KEY=你生成的密钥\n"
            + "\n"
            + "    3) 或通过环境变量启动：\n"
            + "         export SECRET_KEY=你生成的密钥\n"
            + "\n"
            + "  注意：密钥设定后请勿再更改，否则已保存的密码将无法解密。\n"
            + "  （仅本地开发可设 DEBUG=true 跳过此校验）\n"
            + "=" * 68
            + "\n"
        )
        print(banner, file=sys.stderr)

        raise RuntimeError(
            "SECRET_KEY 未设置或强度不足，服务拒绝启动（详见上方提示）"
        )


settings = Settings()
