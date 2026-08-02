"""
应用配置
"""
import os
from datetime import datetime, timezone, timedelta
from pydantic_settings import BaseSettings
from typing import Optional

# 北京时区 (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))


def now_beijing() -> datetime:
    """获取北京时间（UTC+8），返回 naive datetime（所有 DB 时间统一为北京时间）"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


class Settings(BaseSettings):
    """应用配置类"""
    
    # 应用配置
    APP_NAME: str = "数据库同步备份工具"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True
    
    # 服务器配置
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    
    # 数据库配置
    DATABASE_URL: str = "sqlite:///./data/dbsync.db"
    
    # 备份配置
    BACKUP_DIR: str = "./backups"
    MAX_BACKUP_FILES: int = 100
    
    # 日志配置
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "./data/dbsync.log"
    
    # 安全配置（部署时务必通过环境变量 SECRET_KEY 设置一个随机密钥）
    SECRET_KEY: str = "change-me-in-production-env"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
    
    def model_post_init(self, _context):
        """配置加载后的校验"""
        if self.SECRET_KEY == "change-me-in-production-env":
            import sys
            print("[WARNING] 正在使用默认 SECRET_KEY，请在 .env 或环境变量中设置一个随机密钥！", file=sys.stderr)


# 创建全局配置实例
settings = Settings()

# 确保必要的目录存在
os.makedirs(os.path.dirname(settings.DATABASE_URL.replace("sqlite:///", "")), exist_ok=True)
os.makedirs(settings.BACKUP_DIR, exist_ok=True)