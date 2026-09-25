"""
控制面数据库（SQLite）引擎与会话管理。

只用同步 SQLAlchemy：所有 ORM 调用点均在线程或同步路由函数中，
之前那套从未被调用的 async engine 已移除。
"""
import os
from contextlib import contextmanager
from typing import Generator, Iterator

from sqlalchemy import create_engine, event
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


def _sqlite_connect_args() -> dict:
    """
    SQLite 连接参数。

    timeout：写锁等待时长。默认 5 秒在高频写入下不够——同步引擎持续写表状态
    与位点，同时 API 还在响应请求，实测会出现
    「database is locked」并导致接口 500。

    配合下面的 WAL 模式，读写可以并发，写写仍会串行，因此需要足够长的等待。
    """
    return {
        "check_same_thread": False,
        "timeout": 30.0,
    }


engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_sqlite_connect_args() if _is_sqlite else {},
    pool_pre_ping=True,
    echo=False,
)


@event.listens_for(engine, "connect")
def _apply_sqlite_pragmas(dbapi_connection, connection_record):
    """
    SQLite 连接建立时应用的 PRAGMA。

    - journal_mode=WAL：写操作不再阻塞读，显著降低并发场景下的
      「database is locked」。同步引擎持续写位点/表状态时尤其关键。
    - synchronous=NORMAL：WAL 下的推荐值，兼顾安全与性能。
    - busy_timeout：与 connect timeout 对应的 SQLite 层等待。
    """
    if not _is_sqlite:
        return
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()
    except Exception:
        # PRAGMA 失败不应阻断连接，退回到默认行为
        pass


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
