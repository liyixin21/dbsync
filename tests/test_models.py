"""
数据模型与迁移相关测试。

重点验证枚举落库格式——旧实现用 SQLAlchemy 默认行为，把枚举的 **name**
（如 RUNNING）写进列而不是 value（running），导致直接读表数据时含义不符预期，
也让 JSON 序列化与数据库内容不一致。
"""
import pytest
from sqlalchemy import text

from app.core.crypto import CredentialDecryptError, decrypt, encrypt, is_encrypted
from app.models.database import (
    BackupStatus,
    BackupType,
    Database,
    SyncHealth,
    SyncStatus,
    SyncTask,
)


class TestEnumStorage:
    def test_sync_status_stores_value_not_name(self, db_session):
        """落库必须是 'running'，不是 'RUNNING'。"""
        record = SyncTask(
            name="enum-test", source_db_id=1, target_db_id=2,
            status=SyncStatus.RUNNING,
        )
        db_session.add(record)
        db_session.commit()

        raw = db_session.execute(
            text("SELECT status FROM sync_tasks WHERE id = :id"), {"id": record.id}
        ).scalar()
        assert raw == "running", f"落库格式错误: {raw!r}"

    def test_health_stores_value(self, db_session):
        record = SyncTask(
            name="health-test", source_db_id=1, target_db_id=2,
            health=SyncHealth.STALLED,
        )
        db_session.add(record)
        db_session.commit()

        raw = db_session.execute(
            text("SELECT health FROM sync_tasks WHERE id = :id"), {"id": record.id}
        ).scalar()
        assert raw == "stalled"

    def test_roundtrip_reads_back_as_enum(self, db_session):
        record = SyncTask(
            name="roundtrip", source_db_id=1, target_db_id=2,
            status=SyncStatus.FAILED, health=SyncHealth.DEGRADED,
        )
        db_session.add(record)
        db_session.commit()
        db_session.expire_all()

        loaded = db_session.query(SyncTask).filter(SyncTask.id == record.id).first()
        assert loaded.status is SyncStatus.FAILED
        assert loaded.health is SyncHealth.DEGRADED

    def test_backup_type_has_no_incremental(self):
        """增量备份已移除，枚举中不应再有该成员。"""
        members = {m.value for m in BackupType}
        assert members == {"full"}
        assert not hasattr(BackupType, "INCREMENTAL")

    def test_pydantic_serialises_to_value(self, db_session):
        """API 响应里的枚举必须是 value，前端才能直接比较字符串。"""
        from app.api.sync_tasks import SyncTaskResponse

        record = SyncTask(
            name="serialise", source_db_id=1, target_db_id=2,
            status=SyncStatus.RUNNING, health=SyncHealth.HEALTHY,
        )
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)

        # 直接构造响应模型的 status/health 字段
        from unittest.mock import patch

        from app.api import sync_tasks as module

        response = module._to_response(record, db_session)
        assert response.status == "running"
        assert response.health == "healthy"


class TestCrypto:
    def test_encrypt_decrypt_roundtrip(self):
        cipher = encrypt("my-password")
        assert cipher != "my-password"
        assert is_encrypted(cipher)
        assert decrypt(cipher) == "my-password"

    def test_empty_values_pass_through(self):
        assert encrypt("") == ""
        assert decrypt("") == ""

    def test_plaintext_legacy_value_returned_as_is(self):
        """历史明文凭据原样返回，不抛异常。"""
        assert decrypt("plain-old-password") == "plain-old-password"

    def test_corrupted_token_raises(self):
        """
        合法前缀但内容损坏 → 抛明确异常。

        旧实现会静默返回密文本身，随后连接失败并报「密码错误」，
        把「密钥变更」伪装成「密码写错了」，极难排查。
        """
        broken = "gAAAAA" + "x" * 60
        with pytest.raises(CredentialDecryptError):
            decrypt(broken)

    def test_is_encrypted_rejects_plaintext(self):
        assert is_encrypted("just-text") is False
        assert is_encrypted("") is False


class TestSchemaIntegrityOnEncryption:
    def test_password_not_stored_in_plaintext(self, db_session):
        record = Database(
            name="secure", host="h", port=3306, username="u",
            password=encrypt("super-secret"), database_name="d",
        )
        db_session.add(record)
        db_session.commit()

        raw = db_session.execute(
            text("SELECT password FROM databases WHERE id = :id"), {"id": record.id}
        ).scalar()
        assert raw != "super-secret"
        assert "super-secret" not in raw
        assert decrypt(raw) == "super-secret"


class TestModelDefaults:
    def test_sync_task_new_fields_have_defaults(self, db_session):
        record = SyncTask(name="defaults", source_db_id=1, target_db_id=2)
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)

        assert record.applied_events == 0
        assert record.dlq_count == 0
        assert record.auto_start is False
        assert record.health is SyncHealth.UNKNOWN
        assert record.status is SyncStatus.PENDING

    def test_backup_plan_defaults(self, db_session):
        from app.models.database import BackupPlan

        plan = BackupPlan(name="p", database_id=1, schedule_interval=60)
        db_session.add(plan)
        db_session.commit()
        db_session.refresh(plan)

        assert plan.retention_count == 50
        assert plan.is_active is True
        assert plan.backup_type is BackupType.FULL
