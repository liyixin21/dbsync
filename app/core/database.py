"""
控制面数据库（SQLite）引擎与会话管理。

只用同步 SQLAlchemy：所有 ORM 调用点均在线程或同步路由函数中，
之前那套从未被调用的 async engine 已移除。
"""
import os
from contextlib import contextmanager
from typing import Generator, Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings


def _sqlite_path(url: str) -> str | None:
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "", 1)
    return None


def ensure_directories() -> None:
    """
    确保数据目录与备份目录存在。

    注意：makedirs 只会创建目录，不代表当前用户有写权限。
    挂载卷场景下宿主机目录属主常与容器内 UID 不同，
    真正的可写性由 check_writable_paths 在启动期校验。
    """
    db_path = _sqlite_path(settings.DATABASE_URL)
    if db_path:
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    os.makedirs(settings.BACKUP_DIR, exist_ok=True)
    log_dir = os.path.dirname(os.path.abspath(settings.LOG_FILE))
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)


def check_writable_paths() -> list:
    """
    校验关键路径可写性，返回不可写路径的列表。

    在启动早期调用，把权限问题连同修复命令一起提示给用户，
    避免运行到中途才出现难以定位的写入失败。
    """
    targets = []

    db_path = _sqlite_path(settings.DATABASE_URL)
    if db_path:
        targets.append(("数据库目录", os.path.dirname(os.path.abspath(db_path))))
    targets.append(("备份目录", os.path.abspath(settings.BACKUP_DIR)))

    unwritable = []
    for label, path in targets:
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".write-probe")
            with open(probe, "w") as fh:
                fh.write("")
            os.unlink(probe)
        except OSError as exc:
            unwritable.append((label, path, str(exc)))
    return unwritable


ensure_directories()

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=True,
    echo=False,
)

# expire_on_commit=False：路由函数普遍在 commit 后读取对象属性，
# 关闭过期可使对象在会话关闭后仍可安全访问。
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求级会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """后台线程使用的会话上下文，自动提交/回滚。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """建表并执行结构迁移（均幂等）。"""
    from .migrations import run_migrations
    from ..models.database import Base

    Base.metadata.create_all(bind=engine)
    run_migrations(engine)


def check_database() -> None:
    """启动期连通性探测。"""
    from sqlalchemy import text

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
