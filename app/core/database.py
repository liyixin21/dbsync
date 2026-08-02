"""
数据库会话管理
"""
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import Session
from typing import Generator
import os

from .config import settings

# 同步引擎（用于初始化）
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
    echo=False  # SQL 日志由 loguru 统一管理，避免双重输出
)

# 异步引擎（用于API）
if "sqlite" in settings.DATABASE_URL:
    async_database_url = settings.DATABASE_URL.replace("sqlite://", "sqlite+aiosqlite://")
else:
    async_database_url = settings.DATABASE_URL.replace("mysql://", "mysql+aiomysql://")

async_engine = create_async_engine(
    async_database_url,
    echo=False  # SQL 日志由 loguru 统一管理，避免双重输出
)

# 会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
AsyncSessionLocal = sessionmaker(
    async_engine, class_=AsyncSession, expire_on_commit=False
)

# 注意：Base 在 app/models/database.py 中定义，这里不重复声明


def get_db() -> Generator[Session, None, None]:
    """获取同步数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_async_db() -> AsyncSession:
    """获取异步数据库会话"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


def run_migrations():
    """执行数据库迁移：为已有数据库补充新增的列（幂等安全）"""
    inspector = inspect(engine)

    # 迁移 1：为 users 表添加 token_version 列
    if 'users' in inspector.get_table_names():
        cols = [c['name'] for c in inspector.get_columns('users')]
        if 'token_version' not in cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN token_version INTEGER DEFAULT 0"))
                conn.commit()
            print("[Migration] 已添加 users.token_version 列")

    # 迁移 2：backup_history.file_size 的类型升级
    # SQLite 不支持直接修改列类型，但新列已经是 BIGINT，无需额外处理
    # 如有需要，后续可在此处添加更多迁移


def init_db():
    """初始化数据库表"""
    from ..models.database import Base
    Base.metadata.create_all(bind=engine)
    run_migrations()


async def async_init_db():
    """异步初始化数据库表"""
    from ..models.database import Base
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 在同步引擎上运行迁移
    run_migrations()