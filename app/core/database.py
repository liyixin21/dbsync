"""
数据库会话管理
"""
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
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
    echo=settings.DEBUG
)

# 异步引擎（用于API）
if "sqlite" in settings.DATABASE_URL:
    async_database_url = settings.DATABASE_URL.replace("sqlite://", "sqlite+aiosqlite://")
else:
    async_database_url = settings.DATABASE_URL.replace("mysql://", "mysql+aiomysql://")

async_engine = create_async_engine(
    async_database_url,
    echo=settings.DEBUG
)

# 会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
AsyncSessionLocal = sessionmaker(
    async_engine, class_=AsyncSession, expire_on_commit=False
)

# 模型基类
Base = declarative_base()


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


def init_db():
    """初始化数据库表"""
    from ..models.database import Base
    Base.metadata.create_all(bind=engine)


async def async_init_db():
    """异步初始化数据库表"""
    from ..models.database import Base
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)