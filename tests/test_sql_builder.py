"""
SQL 生成测试。

这是同步引擎正确性的第一道关口：旧实现用全列 WHERE 做 UPDATE/DELETE，
并且在 INSERT 失败时把整表拉黑。这些测试直接锁住新行为。
"""
import pytest

from app.services.sync.sql_builder import (
    TableMeta,
    build_delete,
    build_insert,
    build_update,
)


@pytest.fixture()
def users_meta():
    return TableMeta(
        schema="app",
        name="users",
        columns=("id", "name", "email", "created_at"),
        pk_columns=("id",),
        unique_keys=(("email",),),
    )


@pytest.fixture()
def no_pk_meta():
    return TableMeta(
        schema="app",
        name="logs",
        columns=("ts", "level", "message"),
        pk_columns=(),
        unique_keys=(),
    )


@pytest.fixture()
def generated_meta():
    return TableMeta(
        schema="app",
        name="metrics",
        columns=("id", "value", "value_doubled"),
        pk_columns=("id",),
        unique_keys=(),
        generated_columns=frozenset({"value_doubled"}),
    )


# ============================================================ INSERT

class TestInsert:
    def test_is_idempotent(self, users_meta):
        """INSERT 必须带 ON DUPLICATE KEY UPDATE，否则重放会制造主键冲突。"""
        result = build_insert(users_meta, {"id": 1, "name": "a", "email": "a@x.com"})
        assert "INSERT INTO `app`.`users`" in result.sql
        assert "ON DUPLICATE KEY UPDATE" in result.sql
        assert result.params == [1, "a", "a@x.com"]

    def test_odku_excludes_primary_key(self, users_meta):
        """更新子句不应包含主键本身。"""
        result = build_insert(users_meta, {"id": 1, "name": "a"})
        odku = result.sql.split("ON DUPLICATE KEY UPDATE")[1]
        assert "`id`" not in odku
        assert "`name`" in odku

    def test_all_pk_table_still_valid(self):
        """全是主键的关联表也必须生成语法合法的语句。"""
        meta = TableMeta(
            schema="app", name="links", columns=("a_id", "b_id"),
            pk_columns=("a_id", "b_id"), unique_keys=(),
        )
        result = build_insert(meta, {"a_id": 1, "b_id": 2})
        assert "ON DUPLICATE KEY UPDATE" in result.sql
        # 不能出现空的 ODKU 子句
        assert not result.sql.rstrip().endswith("UPDATE")

    def test_null_values_preserved(self, users_meta):
        result = build_insert(users_meta, {"id": 1, "name": None, "email": "x@y.z"})
        assert result.params == [1, None, "x@y.z"]

    def test_generated_columns_excluded(self, generated_meta):
        """生成列不能显式赋值，否则 MySQL 直接报错。"""
        result = build_insert(
            generated_meta, {"id": 1, "value": 5, "value_doubled": 10}
        )
        assert "value_doubled" not in result.sql
        assert result.params == [1, 5]

    def test_empty_row_rejected(self, users_meta):
        with pytest.raises(ValueError):
            build_insert(users_meta, {})


# ============================================================ UPDATE

class TestUpdate:
    def test_uses_primary_key_only(self, users_meta):
        """核心修复：WHERE 只应包含主键，不是所有列。"""
        result = build_update(
            users_meta,
            before={"id": 1, "name": "old", "email": "old@x.com", "created_at": "2024-01-01"},
            after={"id": 1, "name": "new"},
        )
        where = result.sql.split("WHERE")[1]
        assert "`id` = %s" in where
        # 旧实现的症状：全列进 WHERE
        assert "email" not in where
        assert "created_at" not in where
        assert result.sql.startswith("UPDATE `app`.`users` SET")

    def test_params_order_set_then_where(self, users_meta):
        """参数顺序必须是 SET 值在前、WHERE 值在后，否则绑定错位。"""
        result = build_update(
            users_meta,
            before={"id": 7},
            after={"name": "n", "email": "e@x.com"},
        )
        assert result.params == ["n", "e@x.com", 7]

    def test_primary_key_change_uses_old_value_in_where(self, users_meta):
        """主键被修改时，WHERE 必须用旧主键定位目标行。"""
        result = build_update(
            users_meta, before={"id": 1}, after={"id": 2, "name": "renamed"}
        )
        assert result.params[-1] == 1          # WHERE 用旧值
        assert 2 in result.params              # SET 里有新值
        assert "`id` = %s" in result.sql.split("WHERE")[1]

    def test_null_in_locator_uses_is_null(self, users_meta):
        result = build_update(users_meta, before={"id": None}, after={"name": "x"})
        assert "`id` IS NULL" in result.sql
        # IS NULL 不消耗参数
        assert None not in result.params

    def test_no_locator_falls_back_and_flags(self, no_pk_meta):
        """无主键表必须退化并明确标记，供上层写告警。"""
        result = build_update(
            no_pk_meta,
            before={"ts": "2024-01-01", "level": "INFO", "message": "old"},
            after={"message": "new"},
        )
        assert result.degraded is True
        assert result.reason and "无主键" in result.reason
        assert "`ts` = %s" in result.sql.split("WHERE")[1]

    def test_empty_after_rejected(self, users_meta):
        with pytest.raises(ValueError):
            build_update(users_meta, before={"id": 1}, after={})


# ============================================================ DELETE

class TestDelete:
    def test_uses_primary_key_only(self, users_meta):
        result = build_delete(
            users_meta, {"id": 42, "name": "x", "email": "y@z.com"}
        )
        assert result.sql == "DELETE FROM `app`.`users` WHERE `id` = %s"
        assert result.params == [42]

    def test_null_handling(self, users_meta):
        result = build_delete(users_meta, {"id": None})
        assert "IS NULL" in result.sql
        assert result.params == []

    def test_never_generates_unconditional_delete(self, no_pk_meta):
        """退化路径也绝不能生成无 WHERE 的 DELETE。"""
        result = build_delete(
            no_pk_meta, {"ts": "2024-01-01", "level": "INFO", "message": "m"}
        )
        assert result.degraded is True
        assert "WHERE" in result.sql
        assert "1=1" not in result.sql

    def test_empty_row_rejected(self, users_meta):
        with pytest.raises(ValueError):
            build_delete(users_meta, {})


# ============================================================ 定位键选择

class TestLocatorSelection:
    def test_prefers_primary_key(self):
        meta = TableMeta(
            schema="s", name="t", columns=("a", "b", "c"),
            pk_columns=("a",), unique_keys=(("b", "c"),),
        )
        assert meta.locator_columns == ("a",)
        assert meta.has_reliable_locator is True

    def test_falls_back_to_unique_key(self):
        meta = TableMeta(
            schema="s", name="t", columns=("a", "b"),
            pk_columns=(), unique_keys=(("a", "b"),),
        )
        assert meta.locator_columns == ("a", "b")
        assert meta.has_reliable_locator is True

    def test_no_keys_reports_unreliable(self):
        meta = TableMeta(schema="s", name="t", columns=("a",))
        assert meta.locator_columns == ()
        assert meta.has_reliable_locator is False


# ============================================================ 标识符转义

class TestEscaping:
    def test_backticks_in_identifiers_are_escaped(self):
        meta = TableMeta(
            schema="weird", name="tbl", columns=("col`name",), pk_columns=("col`name",)
        )
        result = build_insert(meta, {"col`name": 1})
        assert "`col``name`" in result.sql
