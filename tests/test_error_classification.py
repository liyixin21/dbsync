"""
错误分类与处置语义测试。

核心回归：旧实现把 Duplicate entry 与 doesn't exist 都当成「永久性错误」，
随后把整表加入黑名单，导致该表此后所有变更被静默丢弃。
"""
import mysql.connector
import pytest

from app.services.sync import errors as err
from app.services.sync.errors import ErrorClass


class FakeMySQLException(Exception):
    def __init__(self, errno, message):
        super().__init__(message)
        self.errno = errno


# ============================================================ 分类

class TestClassification:
    @pytest.mark.parametrize(
        "errno,message,expected",
        [
            (1146, "Table 'app.users' doesn't exist", ErrorClass.SCHEMA_MISSING),
            (1054, "Unknown column 'foo' in 'field list'", ErrorClass.SCHEMA_MISSING),
            (1062, "Duplicate entry '1' for key 'PRIMARY'", ErrorClass.DUPLICATE),
            (1406, "Data too long for column 'name'", ErrorClass.DATA),
            (1366, "Incorrect string value", ErrorClass.DATA),
            (1048, "Column 'x' cannot be null", ErrorClass.DATA),
            (2006, "MySQL server has gone away", ErrorClass.CONNECTION),
            (2013, "Lost connection to MySQL server", ErrorClass.CONNECTION),
            (1045, "Access denied for user", ErrorClass.AUTH),
        ],
    )
    def test_maps_errno(self, errno, message, expected):
        assert err.classify_error(FakeMySQLException(errno, message))[0] is expected

    def test_duplicate_is_not_schema_missing(self):
        """最关键的一条：冲突与缺表必须分开。"""
        cls, _ = err.classify_error(
            FakeMySQLException(1062, "Duplicate entry '1' for key 'PRIMARY'")
        )
        assert cls is ErrorClass.DUPLICATE
        assert cls is not ErrorClass.SCHEMA_MISSING

    def test_text_fallback_when_errno_missing(self):
        cls, code = err.classify_error(Exception("Duplicate entry 'x' for key 'k'"))
        assert cls is ErrorClass.DUPLICATE
        assert code is None

    def test_unknown_error_classified_as_unknown(self):
        cls, _ = err.classify_error(Exception("something bizarre happened"))
        assert cls is ErrorClass.UNKNOWN

    def test_extracts_code_from_args(self):
        assert err.extract_error_code(Exception(1062, "dup")) == 1062


# ============================================================ 处置语义

class TestDisposition:
    def test_duplicate_advances_checkpoint(self):
        """
        冲突行必须跳过后继续推进位点，否则同一条坏数据会永久卡死消费。
        """
        assert err.should_advance_checkpoint(ErrorClass.DUPLICATE) is True

    def test_duplicate_does_not_break_table(self):
        """
        旧实现的核心缺陷：一次主键冲突 → 整表永久停摆。
        现在冲突只能影响单行。
        """
        assert err.mark_table_broken(ErrorClass.DUPLICATE) is False
        assert err.mark_table_broken(ErrorClass.DATA) is False

    def test_only_schema_missing_marks_table_broken(self):
        assert err.mark_table_broken(ErrorClass.SCHEMA_MISSING) is True

    def test_schema_missing_advances_checkpoint(self):
        """缺表时位点要推进（DDL 会随后到达），不能永久阻塞。"""
        assert err.should_advance_checkpoint(ErrorClass.SCHEMA_MISSING) is True

    def test_connection_never_advances_checkpoint(self):
        """
        连接类错误绝不能推进位点——否则重启后会跳过一个数据缺口，
        且永远无法恢复。
        """
        assert err.should_advance_checkpoint(ErrorClass.CONNECTION) is False
        assert err.should_advance_checkpoint(ErrorClass.AUTH) is False

    def test_connection_is_retryable(self):
        assert err.should_retry(ErrorClass.CONNECTION) is True

    def test_dlq_targets(self):
        """
        可跳过的错误都要留痕。

        SCHEMA_MISSING 也进 DLQ：这些事件确实没能落到目标库，属于真实的数据缺口。
        连接类/权限类则不进——它们会整体回滚并重试，不是「被跳过的事件」。
        """
        for cls in (ErrorClass.DATA, ErrorClass.DUPLICATE, ErrorClass.FATAL,
                    ErrorClass.UNKNOWN, ErrorClass.SCHEMA_MISSING):
            assert err.to_dlq(cls) is True
        for cls in (ErrorClass.CONNECTION, ErrorClass.AUTH):
            assert err.to_dlq(cls) is False

    def test_real_mysql_errors_classify_correctly(self):
        """用真实的 mysql.connector 异常类型验证一遍映射。"""
        exc = mysql.connector.Error(msg="Duplicate entry", errno=1062)
        assert err.classify_error(exc)[0] is ErrorClass.DUPLICATE

        exc = mysql.connector.Error(msg="Table doesn't exist", errno=1146)
        assert err.classify_error(exc)[0] is ErrorClass.SCHEMA_MISSING
