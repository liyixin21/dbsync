"""
测试环境装配。

环境变量必须在导入 app 之前设置，因为 config.Settings 在模块导入时实例化。
"""
import os
import shutil
import tempfile

# ---------------- 必须在导入应用前完成 ----------------
_TMP_ROOT = tempfile.mkdtemp(prefix="dbsync-test-")
_DB_PATH = os.path.join(_TMP_ROOT, "test.db")

os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["SECRET_KEY"] = "test-secret-key-for-pytest-only-0123456789"
os.environ["DEBUG"] = "true"
os.environ["BACKUP_DIR"] = os.path.join(_TMP_ROOT, "backups")
os.environ["LOG_FILE"] = os.path.join(_TMP_ROOT, "test.log")
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["SYNC_CHECKPOINT_INTERVAL"] = "0.05"
os.environ["LOGIN_MAX_ATTEMPTS"] = "3"
os.environ["LOGIN_WINDOW_SECONDS"] = "60"
# 测试期间不拉真实调度器

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import SessionLocal, engine, init_db  # noqa: E402
from app.models.database import Base, Database, User  # noqa: E402
from app.core.security import hash_password  # noqa: E402

ADMIN_USER = "admin"
ADMIN_PASS = "admin123"


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp():
    """
    会话收尾：停掉运行日志缓冲线程再删临时目录。

    后台 flusher 线程若是给已关闭的数据库写日志，会抛
    "attempt to write a readonly database" 噪声，干扰测试输出的判读。
    """
    yield
    try:
        from app.services.audit import run_log_buffer

        run_log_buffer.stop()
    except Exception:
        pass
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture()
def db_session():
    """每个测试独立的数据库会话。"""
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _fresh_database():
    """每个测试前清空所有表，保证隔离。"""
    init_db()
    session = SessionLocal()
    try:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    finally:
        session.close()
    yield


@pytest.fixture()
def admin_user(db_session):
    user = User(
        username=ADMIN_USER,
        password_hash=hash_password(ADMIN_PASS),
        is_active=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def client(admin_user):
    """带已登录令牌的测试客户端。

    直接构造 lifespan 之外的客户端：后台服务（同步/备份调度）在这里不需要，
    且它们会尝试连真实 MySQL。
    """
    from app.main import app

    with TestClient(app) as _client:
        response = _client.post(
            "/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS}
        )
        assert response.status_code == 200, response.text
        token = response.json()["access_token"]
        _client.headers.update({"Authorization": f"Bearer {token}"})
        _client.app_token = token  # type: ignore[attr-defined]
        yield _client


@pytest.fixture()
def anon_client(admin_user):
    """未认证客户端。"""
    from app.main import app

    with TestClient(app) as _client:
        yield _client


@pytest.fixture()
def sample_database(db_session):
    """一条数据库配置记录。密码为明文占位——测试里不需要真连。"""
    from app.core.crypto import encrypt

    record = Database(
        name="test-db",
        host="127.0.0.1",
        port=3306,
        username="tester",
        password=encrypt("secret"),
        database_name="testdb",
        is_active=True,
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


@pytest.fixture()
def second_database(db_session):
    from app.core.crypto import encrypt

    record = Database(
        name="target-db",
        host="127.0.0.1",
        port=3307,
        username="tester",
        password=encrypt("secret"),
        database_name="targetdb",
        is_active=True,
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record
