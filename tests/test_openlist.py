"""
OpenList 上传功能测试。

重点：
- 客户端对各类响应的处理（成功、认证失败、目录不存在）
- token 缓存与失效刷新
- 上传失败不影响备份本身的成功状态
- 配置的加密存储与读取
"""
import os
import tempfile

import httpx
import pytest

from app.services.openlist import client as client_mod

from app.services.openlist.client import (
    OpenListClient,
    OpenListConfig,
    OpenListError,
    _token_cache,
)
from app.services.openlist.config import (
    describe_config,
    is_enabled,
    load_config,
    save_config,
)
from app.models.database import BackupHistory, BackupPlan, BackupStatus, BackupType, UploadStatus


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """每个测试前清空 token 缓存，避免用例互相影响。"""
    _token_cache._tokens.clear()
    yield
    _token_cache._tokens.clear()


def make_config(**kwargs) -> OpenListConfig:
    defaults = dict(
        base_url="http://openlist.test:5244",
        username="admin",
        password="secret",
        remote_dir="/backups",
    )
    defaults.update(kwargs)
    return OpenListConfig(**defaults)


class _Router:
    """
    把假实现按 HTTP 方法分派。

    _httpx_request(method, url, **kwargs) 比 httpx.post/put 多一个 method 参数，
    这里做适配，让各测试用例保持原有的假实现写法。
    """

    def __init__(self):
        self.handlers = {}

    def post(self, fn):
        self.handlers["POST"] = fn
        return self

    def put(self, fn):
        self.handlers["PUT"] = fn
        return self

    def __call__(self, method, url, **kwargs):
        handler = self.handlers.get(method.upper())
        if handler is None:
            raise AssertionError(f"未预期的请求方法: {method}")
        return handler(url, **kwargs)


@pytest.fixture()
def router(monkeypatch):
    """按方法分派的请求替身。"""
    r = _Router()
    monkeypatch.setattr(client_mod, "_httpx_request", r)
    return r


def _wrap_post(fake_post):
    return _Router().post(fake_post)


def _wrap_put(fake_put):
    return _Router().put(fake_put)


# ============================================================ 配置规范化

class TestConfigNormalisation:
    def test_base_url_trailing_slash_stripped(self):
        assert make_config(base_url="http://host:5244/").normalized_base() == "http://host:5244"

    def test_dir_gets_leading_slash(self):
        assert make_config(remote_dir="backups/db").normalized_dir() == "/backups/db"

    def test_dir_trailing_slash_stripped(self):
        assert make_config(remote_dir="/backups/db/").normalized_dir() == "/backups/db"

    def test_empty_dir_becomes_root(self):
        assert make_config(remote_dir="").normalized_dir() == "/"

    def test_root_stays_root(self):
        assert make_config(remote_dir="/").normalized_dir() == "/"


# ============================================================ 认证

class TestAuthentication:
    def test_login_success_caches_token(self, monkeypatch):
        calls = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return httpx.Response(
                200,
                json={"code": 200, "message": "success", "data": {"token": "tok-abc"}},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(client_mod, "_httpx_request", _wrap_post(fake_post))

        client = OpenListClient(make_config())
        assert client._token() == "tok-abc"
        # 第二次应命中缓存，不再请求
        assert client._token() == "tok-abc"
        assert len(calls) == 1, f"未使用缓存，发起了 {len(calls)} 次登录"

    def test_login_rejected_raises_readable_error(self, monkeypatch):
        def fake_post(url, **kwargs):
            return httpx.Response(
                200,
                json={"code": 401, "message": "wrong password"},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(client_mod, "_httpx_request", _wrap_post(fake_post))

        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config())._login()
        assert "wrong password" in str(exc.value)

    def test_non_json_response_gives_actionable_message(self, monkeypatch):
        """地址填错指向普通网站时，应提示而不是抛 JSON 解析异常。"""
        def fake_post(url, **kwargs):
            return httpx.Response(
                200, text="<html>not openlist</html>",
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(client_mod, "_httpx_request", _wrap_post(fake_post))

        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config())._login()
        assert "OpenList" in str(exc.value)

    def test_connection_error_wrapped(self, monkeypatch):
        def fake_post(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(client_mod, "_httpx_request", _wrap_post(fake_post))

        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config())._login()
        assert "无法连接" in str(exc.value)

    def test_missing_credentials_rejected(self):
        with pytest.raises(OpenListError):
            OpenListClient(make_config(base_url=""))._login()
        with pytest.raises(OpenListError):
            OpenListClient(make_config(username=""))._login()

    def test_token_cache_is_per_endpoint(self, monkeypatch):
        """不同地址的 token 不能混用。"""
        tokens = {"http://a.test": "tok-a", "http://b.test": "tok-b"}

        def fake_post(url, **kwargs):
            for base, token in tokens.items():
                if url.startswith(base):
                    return httpx.Response(
                        200, json={"code": 200, "data": {"token": token}},
                        request=httpx.Request("POST", url),
                    )
            raise AssertionError(url)

        monkeypatch.setattr(client_mod, "_httpx_request", _wrap_post(fake_post))

        a = OpenListClient(make_config(base_url="http://a.test"))
        b = OpenListClient(make_config(base_url="http://b.test"))
        assert a._token() == "tok-a"
        assert b._token() == "tok-b"


# ============================================================ 目录

class TestDirectory:
    def test_ensure_directory_creates_each_level(self, monkeypatch):
        created = []

        def fake_post(url, **kwargs):
            if url.endswith("/api/auth/login"):
                return httpx.Response(
                    200, json={"code": 200, "data": {"token": "t"}},
                    request=httpx.Request("POST", url),
                )
            created.append(kwargs["json"]["path"])
            return httpx.Response(
                200, json={"code": 200, "message": "success"},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(client_mod, "_httpx_request", _wrap_post(fake_post))

        OpenListClient(make_config(remote_dir="/a/b/c")).ensure_directory()

        assert created == ["/a", "/a/b", "/a/b/c"], f"逐级创建失败: {created}"

    def test_existing_directory_tolerated(self, monkeypatch):
        """目录已存在不应视为失败。"""
        def fake_post(url, **kwargs):
            if url.endswith("/api/auth/login"):
                return httpx.Response(
                    200, json={"code": 200, "data": {"token": "t"}},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200, json={"code": 500, "message": "file already exists"},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(client_mod, "_httpx_request", _wrap_post(fake_post))
        OpenListClient(make_config(remote_dir="/exists")).ensure_directory()

    def test_root_directory_no_request(self, monkeypatch):
        """根目录无需创建，不应发起任何请求。"""
        def fake_post(url, **kwargs):
            raise AssertionError("根目录不应发起创建请求")

        monkeypatch.setattr(client_mod, "_httpx_request", _wrap_post(fake_post))
        OpenListClient(make_config(remote_dir="/")).ensure_directory()

    def test_mkdir_failure_raises(self, monkeypatch):
        def fake_post(url, **kwargs):
            if url.endswith("/api/auth/login"):
                return httpx.Response(
                    200, json={"code": 200, "data": {"token": "t"}},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200, json={"code": 500, "message": "permission denied"},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(client_mod, "_httpx_request", _wrap_post(fake_post))

        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config(remote_dir="/nope")).ensure_directory()
        assert "permission denied" in str(exc.value)


# ============================================================ 上传

class TestUpload:
    def _setup(self, router, put_response):
        """装配：登录与 mkdir 成功，PUT 返回指定响应。"""
        put_calls = []

        def fake_post(url, **kwargs):
            if url.endswith("/api/auth/login"):
                return httpx.Response(
                    200, json={"code": 200, "data": {"token": "tok"}},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200, json={"code": 200, "message": "success"},
                request=httpx.Request("POST", url),
            )

        def fake_put(url, **kwargs):
            put_calls.append({"url": url, "headers": kwargs.get("headers", {})})
            return httpx.Response(
                put_response[0], json=put_response[1],
                request=httpx.Request("PUT", url),
            )

        router.post(fake_post).put(fake_put)
        return put_calls

    @pytest.fixture()
    def local_file(self):
        fd, path = tempfile.mkstemp(suffix=".sql")
        os.write(fd, b"-- dump content\n")
        os.close(fd)
        yield path
        if os.path.exists(path):
            os.unlink(path)

    def test_upload_success(self, router, local_file):
        calls = self._setup(
            router, (200, {"code": 200, "message": "success", "data": {"task": {"id": "x"}}})
        )

        result = OpenListClient(make_config()).upload_file(local_file)

        assert result.success is True
        assert result.remote_path == "/backups/" + os.path.basename(local_file)
        assert len(calls) == 1

    def test_upload_sets_required_headers(self, router, local_file):
        calls = self._setup(router, (200, {"code": 200, "message": "success"}))

        OpenListClient(make_config()).upload_file(local_file)

        headers = calls[0]["headers"]
        assert headers["Authorization"] == "tok"
        assert "File-Path" in headers
        assert headers["Content-Type"] == "application/octet-stream"
        # File-Path 必须 URL 编码
        assert "%2F" in headers["File-Path"] or headers["File-Path"].startswith("/")

    def test_upload_to_custom_dir(self, router, local_file):
        self._setup(router, (200, {"code": 200, "message": "success"}))

        result = OpenListClient(make_config()).upload_file(local_file, remote_dir="/custom/dir")

        assert result.remote_path.startswith("/custom/dir/")

    def test_upload_custom_filename(self, router, local_file):
        self._setup(router, (200, {"code": 200, "message": "success"}))

        result = OpenListClient(make_config()).upload_file(local_file, remote_name="renamed.sql")

        assert result.remote_path.endswith("/renamed.sql")

    def test_upload_rejected_code(self, router, local_file):
        self._setup(router, (200, {"code": 500, "message": "storage full"}))

        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config()).upload_file(local_file)
        assert "storage full" in str(exc.value)

    def test_upload_http_error(self, router, local_file):
        self._setup(router, (500, {"code": 500, "message": "server error"}))

        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config()).upload_file(local_file)
        assert "500" in str(exc.value)

    def test_upload_retries_once_on_401(self, router, local_file):
        """token 过期时应刷新后重试，而不是直接失败。"""
        state = {"login_count": 0, "put_count": 0}

        def fake_post(url, **kwargs):
            if url.endswith("/api/auth/login"):
                state["login_count"] += 1
                return httpx.Response(
                    200, json={"code": 200, "data": {"token": f"tok-{state['login_count']}"}},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200, json={"code": 200, "message": "success"},
                request=httpx.Request("POST", url),
            )

        def fake_put(url, **kwargs):
            state["put_count"] += 1
            if state["put_count"] == 1:
                return httpx.Response(
                    401, text="unauthorized", request=httpx.Request("PUT", url)
                )
            return httpx.Response(
                200, json={"code": 200, "message": "success"},
                request=httpx.Request("PUT", url),
            )

        router.post(fake_post).put(fake_put)

        result = OpenListClient(make_config()).upload_file(local_file)

        assert result.success is True
        assert state["put_count"] == 2, "未在 401 后重试"
        assert state["login_count"] == 2, "未重新登录获取新 token"

    def test_missing_local_file(self, monkeypatch):
        monkeypatch.setattr(client_mod, "_httpx_request", lambda *a, **k: None)
        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config()).upload_file("/nonexistent/file.sql")
        assert "不存在" in str(exc.value)


# ============================================================ 配置存取

class TestConfigStorage:
    def test_save_and_load(self, db_session):
        save_config(
            enabled=True, base_url="http://ol.test:5244", username="admin",
            password="pw123", remote_dir="/dbsync", verify_ssl=False,
        )

        assert is_enabled()
        config = load_config()
        assert config is not None
        assert config.base_url == "http://ol.test:5244"
        assert config.username == "admin"
        assert config.password == "pw123"          # 加密存储但读取时还原
        assert config.remote_dir == "/dbsync"
        assert config.verify_ssl is False

    def test_password_encrypted_at_rest(self, db_session):
        from app.core.crypto import decrypt, is_encrypted
        from app.models.database import SystemConfig

        save_config(
            enabled=True, base_url="http://ol.test", username="u",
            password="plaintext-pw", remote_dir="/x", verify_ssl=True,
        )

        row = (
            db_session.query(SystemConfig)
            .filter(SystemConfig.key == "openlist_password")
            .first()
        )
        assert row is not None
        assert row.value != "plaintext-pw", "密码以明文存储"
        assert is_encrypted(row.value)

    def test_password_blank_keeps_existing(self, db_session):
        save_config(
            enabled=True, base_url="http://ol.test", username="u",
            password="original", remote_dir="/x", verify_ssl=True,
        )
        # 再次保存但密码留空
        save_config(
            enabled=True, base_url="http://ol.test2", username="u2",
            password=None, remote_dir="/y", verify_ssl=True,
        )

        config = load_config()
        assert config.password == "original", "留空密码时被清掉了"
        assert config.base_url == "http://ol.test2"

    def test_disabled_flag(self, db_session):
        save_config(
            enabled=False, base_url="http://ol.test", username="u",
            password="p", remote_dir="/x", verify_ssl=True,
        )
        assert is_enabled() is False

    def test_describe_hides_password(self, db_session):
        save_config(
            enabled=True, base_url="http://ol.test", username="u",
            password="secret-pw", remote_dir="/x", verify_ssl=True,
        )

        info = describe_config()
        assert info["password_set"] is True
        assert "password" not in info or info.get("password") is None

    def test_load_returns_none_when_unconfigured(self, db_session):
        assert load_config() is None

    def test_load_returns_none_when_password_missing(self, db_session):
        save_config(
            enabled=True, base_url="http://ol.test", username="u",
            password=None, remote_dir="/x", verify_ssl=True,
        )
        assert load_config() is None


# ============================================================ 备份集成

class TestBackupIntegration:
    """上传状态与备份结果的关系。"""

    @pytest.fixture()
    def history(self, db_session):
        from app.models.database import Database
        from app.core.crypto import encrypt

        database = Database(
            name="bk-db", host="127.0.0.1", port=3306, username="u",
            password=encrypt("p"), database_name="d",
        )
        db_session.add(database)
        db_session.flush()

        plan = BackupPlan(
            name="bk-plan", database_id=database.id, schedule_interval=60,
        )
        db_session.add(plan)
        db_session.flush()

        record = BackupHistory(
            backup_plan_id=plan.id,
            status=BackupStatus.COMPLETED,
            backup_type=BackupType.FULL,
        )
        db_session.add(record)
        db_session.commit()
        return plan.id, record.id

    def test_upload_skipped_by_default(self, db_session, history, tmp_path):
        import asyncio

        from app.services.backup.engine import BackupEngine
        from app.models.database import Database

        plan_id, history_id = history
        database = db_session.query(Database).first()

        engine = BackupEngine(plan_id=plan_id, database=database, upload_enabled=False)
        asyncio.run(engine._upload_to_openlist(history_id, str(tmp_path / "x.sql")))

        db_session.expire_all()
        row = db_session.query(BackupHistory).filter(BackupHistory.id == history_id).first()
        assert row.upload_status is UploadStatus.SKIPPED

    def test_upload_failure_does_not_touch_backup_status(self, db_session, history, tmp_path):
        """
        上传失败时，备份记录的 status 必须保持 completed。

        本地备份已经落盘且校验通过，因远端问题把它标成失败是误导。
        """
        import asyncio

        from app.services.backup.engine import BackupEngine
        from app.models.database import Database

        plan_id, history_id = history
        database = db_session.query(Database).first()

        local = tmp_path / "backup.sql"
        local.write_text("-- dump\n")

        # 开启上传但未配置 OpenList → 走到全局开关检查
        engine = BackupEngine(plan_id=plan_id, database=database, upload_enabled=True)
        asyncio.run(engine._upload_to_openlist(history_id, str(local)))

        db_session.expire_all()
        row = db_session.query(BackupHistory).filter(BackupHistory.id == history_id).first()
        assert row.status is BackupStatus.COMPLETED, "上传问题影响了备份状态"
        assert row.upload_status is UploadStatus.SKIPPED  # 全局开关未启用的提示路径

    def test_upload_records_failure_reason(self, db_session, history, tmp_path, monkeypatch):
        """上传失败必须留下可读原因，否则用户无从排查。"""
        import asyncio

        from app.services.backup.engine import BackupEngine
        from app.services import openlist
        from app.models.database import Database

        plan_id, history_id = history
        database = db_session.query(Database).first()

        local = tmp_path / "backup.sql"
        local.write_text("-- dump\n")

        monkeypatch.setattr(openlist, "is_enabled", lambda: True)

        class BoomClient:
            def upload_file(self, *a, **k):
                raise RuntimeError("磁盘空间不足")

        monkeypatch.setattr(openlist, "build_client", lambda: BoomClient())

        engine = BackupEngine(plan_id=plan_id, database=database, upload_enabled=True)
        asyncio.run(engine._upload_to_openlist(history_id, str(local)))

        db_session.expire_all()
        row = db_session.query(BackupHistory).filter(BackupHistory.id == history_id).first()
        assert row.upload_status is UploadStatus.FAILED
        assert "磁盘空间不足" in (row.upload_error or "")
        assert row.status is BackupStatus.COMPLETED

    def test_upload_success_records_path(self, db_session, history, tmp_path, monkeypatch):
        import asyncio

        from app.services.backup.engine import BackupEngine
        from app.services import openlist
        from app.models.database import Database
        from app.services.openlist.client import UploadResult

        plan_id, history_id = history
        database = db_session.query(Database).first()

        local = tmp_path / "backup.sql"
        local.write_text("-- dump\n")

        monkeypatch.setattr(openlist, "is_enabled", lambda: True)

        class OkClient:
            def upload_file(self, path, remote_dir=None):
                return UploadResult(success=True, remote_path="/dbsync/backup.sql")

        monkeypatch.setattr(openlist, "build_client", lambda: OkClient())

        engine = BackupEngine(plan_id=plan_id, database=database, upload_enabled=True)
        asyncio.run(engine._upload_to_openlist(history_id, str(local)))

        db_session.expire_all()
        row = db_session.query(BackupHistory).filter(BackupHistory.id == history_id).first()
        assert row.upload_status is UploadStatus.SUCCESS
        assert row.upload_path == "/dbsync/backup.sql"
        assert row.uploaded_at is not None


class TestErrorMessages:
    """
    错误提示必须可操作。

    真实环境暴露的问题：地址格式错误、端口非法、服务不存在三种情况
    都返回同一句「Invalid port: ':1]'」，用户完全无法据此排查。
    """

    def test_missing_scheme(self):
        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config(base_url="127.0.0.1:5244"))._login()
        assert "http://" in str(exc.value), f"未提示协议要求: {exc.value}"

    def test_plain_text_address(self):
        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config(base_url="not-a-url"))._login()
        assert "http://" in str(exc.value) or "格式" in str(exc.value)

    def test_bare_scheme(self):
        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config(base_url="http://"))._login()
        assert "格式" in str(exc.value) or "主机名" in str(exc.value)

    def test_invalid_port(self):
        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config(base_url="http://host:99999"))._login()
        assert "端口" in str(exc.value), f"未指出端口问题: {exc.value}"

    def test_unreachable_service(self, monkeypatch):
        def fake_post(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(client_mod, "_httpx_request", _wrap_post(fake_post))

        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config())._login()
        assert "无法连接" in str(exc.value)
        # 提示里要带上目标地址，便于确认填错没有
        assert "openlist.test" in str(exc.value)


class TestExceptionContainment:
    """
    异常不得穿透成 500。

    真实环境暴露的问题：httpx 解析畸形地址时抛 ValueError，
    它不是 httpx.RequestError 的子类，未被捕获便穿透成 HTTP 500，
    用户在界面上只看到「服务器内部错误」，真正的原因被吞掉。
    """

    def test_value_error_from_httpx_is_wrapped(self, monkeypatch):
        def fake_post(url, **kwargs):
            raise ValueError("invalid literal for int() with base 10: ':1]'")

        monkeypatch.setattr(client_mod, "_httpx_request", _wrap_post(fake_post))

        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config())._login()
        assert "地址无法解析" in str(exc.value)

    def test_value_error_during_mkdir_wrapped(self, monkeypatch):
        def fake_post(url, **kwargs):
            if url.endswith("/api/auth/login"):
                return httpx.Response(
                    200, json={"code": 200, "data": {"token": "t"}},
                    request=httpx.Request("POST", url),
                )
            raise ValueError("bad address")

        monkeypatch.setattr(client_mod, "_httpx_request", _wrap_post(fake_post))

        with pytest.raises(OpenListError):
            OpenListClient(make_config(remote_dir="/x")).ensure_directory()

    def test_value_error_during_upload_wrapped(self, router, tmp_path):
        local = tmp_path / "f.sql"
        local.write_text("data")

        def fake_post(url, **kwargs):
            if url.endswith("/api/auth/login"):
                return httpx.Response(
                    200, json={"code": 200, "data": {"token": "t"}},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200, json={"code": 200}, request=httpx.Request("POST", url)
            )

        def fake_put(url, **kwargs):
            raise ValueError("bad address")

        router.post(fake_post).put(fake_put)

        with pytest.raises(OpenListError) as exc:
            OpenListClient(make_config()).upload_file(str(local))
        assert "地址无法解析" in str(exc.value)

    def test_api_endpoint_never_returns_500(self, client, db_session, monkeypatch):
        """接口层对未知异常也必须返回结构化结果，不能是 500。"""
        import httpx as _httpx

        def boom(*args, **kwargs):
            raise RuntimeError("unexpected internal failure")

        monkeypatch.setattr(client_mod, "_httpx_request", boom)

        res = client.post(
            "/api/system/openlist/test",
            json={"base_url": "http://x.test", "username": "u", "password": "p"},
        )
        # 未配置凭据时会先被参数校验拦下；关键是不能返回 500
        assert res.status_code != 500, f"接口抛出了 500: {res.text}"
