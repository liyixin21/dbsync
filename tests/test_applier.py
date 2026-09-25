"""
应用器测试。

用假的数据库连接驱动真实的 Applier，验证：
- 事务边界（XID 前不提交）
- 幂等重放
- 错误分级处置：冲突/数据错误跳过并进 DLQ，连接错误整体回滚
- 表状态上报
- 无主键表退化告警
"""
import pytest

from app.services.sync import errors as err
from app.services.sync.applier import Applier, TransientConnectionError
from app.services.sync.sql_builder import TableMeta, build_insert


# ============================================================ 测试替身

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0
        self._pending = None

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if self.conn.fail_on and self.conn.fail_on[0] in sql:
            exc = self.conn.fail_on[1]
            self.conn.fail_on = None
            raise exc
        self.rowcount = self.conn.rowcount_for(sql)

    def close(self):
        pass

    def fetchall(self):
        return []


class FakeConnection:
    """记录所有执行与提交行为的最小连接替身。"""

    def __init__(self, fail_on=None, rowcount=1):
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.autocommit = True
        self.fail_on = fail_on
        self._rowcount = rowcount

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def rowcount_for(self, sql):
        return self._rowcount


class FakeResolver:
    """表结构解析替身。"""

    def __init__(self, metas):
        self.metas = metas
        self.invalidated = []

    def get(self, schema, table):
        return self.metas.get((schema, table))

    def invalidate(self, schema, table=None):
        self.invalidated.append((schema, table))


USERS = TableMeta(
    schema="app", name="users",
    columns=("id", "name", "email"), pk_columns=("id",),
)
LOGS = TableMeta(
    schema="app", name="logs", columns=("ts", "msg"), pk_columns=(), unique_keys=(),
)


def make_applier(conn, metas=None, **kwargs):
    """装配一个使用假连接的 Applier，并收集回调数据。"""
    dlq = []
    tables = []

    def dlq_recorder(event):
        dlq.append(event)

    def table_recorder(schema, table, state, file, pos, delta, message):
        tables.append(
            {"schema": schema, "table": table, "state": state, "delta": delta,
             "message": message}
        )

    applier = Applier(
        task_id=1,
        connection_provider=lambda: conn,
        schema_resolver=metas or FakeResolver({("app", "users"): USERS}),
        dlq_recorder=dlq_recorder,
        table_state_recorder=table_recorder,
        tx_max_rows=kwargs.pop("tx_max_rows", 10000),
    )
    return applier, dlq, tables


# ============================================================ 事务边界

class TestTransactionBoundary:
    def test_nothing_committed_before_xid(self):
        """XID 之前绝不能提交——这是源库事务在目标库保持原子的前提。"""
        conn = FakeConnection()
        applier, _, _ = make_applier(conn)

        applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})
        applier.apply_insert("app", "users", {"id": 2, "name": "b", "email": "f"})

        assert len(conn.executed) == 2
        assert conn.commits == 0, "事务未结束就提交了"

    def test_commit_on_xid_flushes_once(self):
        conn = FakeConnection()
        applier, _, _ = make_applier(conn)

        applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})
        applier.apply_insert("app", "users", {"id": 2, "name": "b", "email": "f"})
        stats = applier.commit()

        assert conn.commits == 1
        assert stats.inserted == 2

    def test_rollback_discards_pending(self):
        conn = FakeConnection()
        applier, _, _ = make_applier(conn)

        applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})
        applier.rollback()

        assert conn.rollbacks == 1
        assert conn.commits == 0

    def test_transaction_resets_after_commit(self):
        conn = FakeConnection()
        applier, _, _ = make_applier(conn)

        applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})
        applier.commit()
        applier.apply_insert("app", "users", {"id": 2, "name": "b", "email": "f"})

        assert len(conn.executed) == 2
        assert conn.commits == 1

    def test_large_transaction_guard_splits(self):
        """超大事务提前提交，牺牲原子性换取资源安全，但必须有据可查。"""
        conn = FakeConnection()
        applier, _, _ = make_applier(conn, tx_max_rows=3)

        for i in range(3):
            applier.apply_insert(
                "app", "users", {"id": i, "name": f"u{i}", "email": f"{i}@x.com"}
            )

        assert conn.commits == 1, "超过阈值应提前提交"

    def test_autocommit_disabled_on_connect(self):
        conn = FakeConnection()
        applier, _, _ = make_applier(conn)
        applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})
        assert conn.autocommit is False


# ============================================================ 幂等

class TestIdempotency:
    def test_insert_sql_is_idempotent(self):
        """重放同一事件多次，SQL 本身必须无副作用。"""
        conn = FakeConnection()
        applier, _, _ = make_applier(conn)
        row = {"id": 1, "name": "a", "email": "e@x.com"}

        applier.apply_insert("app", "users", row)
        applier.apply_insert("app", "users", row)

        sqls = [sql for sql, _ in conn.executed]
        assert len(sqls) == 2
        assert all("ON DUPLICATE KEY UPDATE" in sql for sql in sqls)

    def test_replay_does_not_raise_on_duplicate(self):
        """
        崩溃后从位点重放：目标库已有该行会产生 1062。
        旧实现会因此把整表拉黑；新实现走 ON DUPLICATE KEY UPDATE，
        实际上连错误都不会出现。
        """
        conn = FakeConnection()
        applier, dlq, _ = make_applier(conn)
        applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})
        applier.commit()
        assert dlq == []


# ============================================================ 错误处置

class TestErrorHandling:
    def test_schema_missing_marks_table_and_does_not_break_flow(self):
        """目标库缺表：标记表状态、记 DLQ、继续处理后续事件。"""
        import mysql.connector

        exc = mysql.connector.Error(msg="Table doesn't exist", errno=1146)
        conn = FakeConnection(fail_on=("INSERT INTO `app`.`users`", exc))
        applier, dlq, tables = make_applier(conn)

        ok = applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})

        assert ok is False
        assert len(dlq) == 1
        assert dlq[0].error_class is err.ErrorClass.SCHEMA_MISSING
        # 表被标记为 schema_missing，等待 DDL 重建
        assert any(t["state"] == "schema_missing" for t in tables)
        # 结构变化后必须清缓存
        assert ("app", "users") in applier._schema.invalidated

    def test_execution_time_schema_missing_also_goes_to_dlq(self):
        """
        回归测试：执行期才发现表不存在（元数据缓存里还有该表）时，
        这条事件同样必须进 DLQ。

        否则「因缺表而未应用的数据」会无痕消失，运维无从知道丢了哪些行。
        这是新引擎里两条不同代码路径的语义一致性要求。
        """
        import mysql.connector

        exc = mysql.connector.Error(msg="Table 'app.users' doesn't exist", errno=1146)
        conn = FakeConnection(fail_on=("INSERT INTO", exc))
        applier, dlq, tables = make_applier(conn)

        ok = applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})

        assert ok is False
        assert len(dlq) == 1, "执行期缺表事件必须记入 DLQ"
        assert dlq[0].error_class is err.ErrorClass.SCHEMA_MISSING
        assert any(t["state"] == "schema_missing" for t in tables)

    def test_schema_missing_counts_toward_failed(self):
        """缺表事件必须计入 failed，否则前端的「未应用」计数会少算。"""
        import mysql.connector

        exc = mysql.connector.Error(msg="Table doesn't exist", errno=1146)
        conn = FakeConnection(fail_on=("INSERT INTO", exc))
        applier, _, _ = make_applier(conn)

        applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})
        stats = applier.commit()

        assert stats.failed == 1
        assert stats.inserted == 0

    def test_duplicate_entry_recorded_as_dlq_not_table_blacklist(self):
        """
        回归测试：主键冲突只能影响该行，绝不能把整表停摆。
        """
        import mysql.connector

        exc = mysql.connector.Error(msg="Duplicate entry '1' for key 'PRIMARY'", errno=1062)
        conn = FakeConnection(fail_on=("INSERT INTO", exc))
        applier, dlq, tables = make_applier(conn)

        ok = applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})

        assert ok is False
        assert len(dlq) == 1
        assert dlq[0].error_class is err.ErrorClass.DUPLICATE
        # 关键：没有任何 schema_missing 标记，表仍可继续接收事件
        assert not any(t["state"] == "schema_missing" for t in tables)

    def test_after_duplicate_next_event_still_applies(self):
        """冲突之后再来的事件必须正常工作——旧实现在这里会静默丢弃。"""
        import mysql.connector

        exc = mysql.connector.Error(msg="Duplicate entry", errno=1062)
        conn = FakeConnection(fail_on=("INSERT INTO", exc))
        applier, dlq, _ = make_applier(conn)

        first = applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})
        second = applier.apply_insert("app", "users", {"id": 2, "name": "b", "email": "f"})

        assert first is False
        assert second is True, "冲突后的后续事件被丢弃了"

    def test_connection_error_raises_and_rolls_back(self):
        """连接类错误必须整体回滚并上抛，由引擎重连，位点不推进。"""
        import mysql.connector

        exc = mysql.connector.Error(msg="MySQL server has gone away", errno=2006)
        conn = FakeConnection(fail_on=("INSERT INTO", exc))
        applier, _, _ = make_applier(conn)

        with pytest.raises(TransientConnectionError):
            applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})

        assert conn.rollbacks == 1
        assert conn.commits == 0

    def test_data_too_long_goes_to_dlq_and_continues(self):
        import mysql.connector

        exc = mysql.connector.Error(msg="Data too long for column 'name'", errno=1406)
        conn = FakeConnection(fail_on=("INSERT INTO", exc))
        applier, dlq, _ = make_applier(conn)

        assert applier.apply_insert(
            "app", "users", {"id": 1, "name": "x" * 500, "email": "e"}
        ) is False
        assert len(dlq) == 1
        assert dlq[0].error_class is err.ErrorClass.DATA

    def test_unknown_table_skipped_with_schema_missing(self):
        """源库表在目标库元数据里查不到：标记等待 DDL，而不是永久放弃。"""
        conn = FakeConnection()
        applier, dlq, tables = make_applier(conn)

        ok = applier.apply_insert("app", "ghost", {"id": 1})

        assert ok is False
        assert dlq[0].error_class is err.ErrorClass.SCHEMA_MISSING
        assert tables[0]["state"] == "schema_missing"
        assert conn.executed == [], "表不存在时不应尝试执行 SQL"

    def test_dlq_not_flooded_within_same_transaction(self):
        """同一事务内同表同类错误只记一次，避免异常风暴刷爆 DLQ。"""
        import mysql.connector

        exc = mysql.connector.Error(msg="Data too long", errno=1406)
        conn = FakeConnection(fail_on=("INSERT INTO", exc))
        applier, dlq, _ = make_applier(conn)

        for i in range(5):
            # 每次重置失败注入，模拟持续的数据错误
            conn.fail_on = ("INSERT INTO", mysql.connector.Error(msg="Data too long", errno=1406))
            applier.apply_insert("app", "users", {"id": i, "name": "x", "email": "e"})

        assert len(dlq) == 1, f"重复记录 {len(dlq)} 次"

    def test_dlq_resets_across_transactions(self):
        import mysql.connector

        conn = FakeConnection()
        applier, dlq, _ = make_applier(conn)

        conn.fail_on = ("INSERT INTO", mysql.connector.Error(msg="Data too long", errno=1406))
        applier.apply_insert("app", "users", {"id": 1, "name": "x", "email": "e"})
        applier.commit()

        conn.fail_on = ("INSERT INTO", mysql.connector.Error(msg="Data too long", errno=1406))
        applier.apply_insert("app", "users", {"id": 2, "name": "x", "email": "e"})

        assert len(dlq) == 2, "跨事务的同类错误应各自记录"


# ============================================================ 事件类型

class TestEventTypes:
    def test_update_before_and_after(self):
        conn = FakeConnection()
        applier, _, _ = make_applier(conn)

        ok = applier.apply_update(
            "app", "users",
            before={"id": 1, "name": "old", "email": "e"},
            after={"id": 1, "name": "new", "email": "e2"},
        )

        assert ok is True
        sql, params = conn.executed[0]
        assert sql.startswith("UPDATE `app`.`users` SET")
        assert "WHERE `id` = %s" in sql
        assert params[-1] == 1

    def test_delete_uses_pk(self):
        conn = FakeConnection()
        applier, _, _ = make_applier(conn)

        ok = applier.apply_delete("app", "users", {"id": 9, "name": "x", "email": "e"})

        assert ok is True
        sql, params = conn.executed[0]
        assert sql == "DELETE FROM `app`.`users` WHERE `id` = %s"
        assert params == [9]

    def test_no_pk_table_flags_degraded(self):
        conn = FakeConnection()
        metas = FakeResolver({("app", "logs"): LOGS, ("app", "users"): USERS})
        applier, _, _ = make_applier(conn, metas=metas)

        ok = applier.apply_update(
            "app", "logs", before={"ts": "t", "msg": "a"}, after={"msg": "b"}
        )

        assert ok is True
        sql, _ = conn.executed[0]
        # 退化到全列匹配，但语句仍然有 WHERE
        assert "WHERE" in sql
        assert "`ts` = %s" in sql

    def test_ddl_clears_cache_and_commits(self):
        conn = FakeConnection()
        metas = FakeResolver({("app", "users"): USERS})
        applier, _, _ = make_applier(conn, metas=metas)

        ok = applier.apply_ddl(
            "ALTER TABLE `app`.`users` ADD COLUMN age INT", "app", "users"
        )

        assert ok is True
        assert ("app", "users") in metas.invalidated
        assert conn.commits >= 1

    def test_ddl_commits_pending_transaction_first(self):
        """DDL 会隐式提交，因此必须先提交已累积的变更。"""
        conn = FakeConnection()
        applier, _, _ = make_applier(conn)

        applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})
        applier.apply_ddl("CREATE TABLE t (id INT)", "app", "t")

        assert conn.commits >= 2

    def test_close_rolls_back_uncommitted(self):
        conn = FakeConnection()
        applier, _, _ = make_applier(conn)

        applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})
        applier.close()

        assert conn.rollbacks == 1
        assert conn.closed is True


# ============================================================ 表状态上报

class TestTableStateReporting:
    def test_success_reports_active_with_delta(self):
        conn = FakeConnection()
        applier, _, tables = make_applier(conn)

        applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})

        successes = [t for t in tables if t["state"] == "active"]
        assert len(successes) == 1
        assert successes[0]["delta"] == 1

    def test_failure_reports_message(self):
        import mysql.connector

        exc = mysql.connector.Error(msg="Table doesn't exist", errno=1146)
        conn = FakeConnection(fail_on=("INSERT INTO", exc))
        applier, _, tables = make_applier(conn)

        applier.apply_insert("app", "users", {"id": 1, "name": "a", "email": "e"})

        failures = [t for t in tables if t["message"]]
        assert len(failures) == 1
        assert failures[0]["state"] == "schema_missing"


class TestColumnMapping:
    """
    源库 binlog_row_metadata=MINIMAL 时的列名还原。

    真实环境暴露的问题：binlog_row_metadata 为 MINIMAL（MySQL 8.0 默认值）时，
    binlog 只记录列的顺序不记录列名，pymysqlreplication 给出 UNKNOWN_COL0
    这类占位键；直接拿去构造 SQL 会报 1054 Unknown column。
    """

    def test_detects_placeholder_keys(self):
        from app.services.sync.applier import ColumnMapper

        assert ColumnMapper.needs_mapping({"UNKNOWN_COL0": 1, "UNKNOWN_COL1": 2})
        assert ColumnMapper.needs_mapping({0: 1, 1: 2})
        assert ColumnMapper.needs_mapping({"0": 1, "1": 2})
        # 已经带真实列名时不应触碰
        assert not ColumnMapper.needs_mapping({"id": 1, "name": "a"})
        assert not ColumnMapper.needs_mapping({})

    def test_remaps_by_column_order(self):
        from app.services.sync.applier import ColumnMapper

        mapped = ColumnMapper.remap(
            {"UNKNOWN_COL0": 1, "UNKNOWN_COL1": "张三", "UNKNOWN_COL2": "z@x.com"},
            ("id", "name", "email"),
        )
        assert mapped == {"id": 1, "name": "张三", "email": "z@x.com"}

    def test_remaps_integer_keys_preserving_order(self):
        """整数键必须按序号排序，不能依赖字典迭代顺序。"""
        from app.services.sync.applier import ColumnMapper

        # 故意打乱插入顺序
        mapped = ColumnMapper.remap({2: "c", 0: "a", 1: "b"}, ("c1", "c2", "c3"))
        assert mapped == {"c1": "a", "c2": "b", "c3": "c"}

    def test_overflow_columns_are_flagged_not_dropped(self):
        """字段数超出表定义时不能静默丢弃。"""
        from app.services.sync.applier import ColumnMapper

        mapped = ColumnMapper.remap(
            {"UNKNOWN_COL0": 1, "UNKNOWN_COL1": 2, "UNKNOWN_COL2": 3},
            ("id", "name"),
        )
        assert mapped["id"] == 1
        assert mapped["name"] == 2
        assert any(str(k).startswith("__overflow_") for k in mapped), "超出字段被静默丢弃"

    def test_end_to_end_insert_with_placeholder_keys(self):
        """走完整 Applier 路径：占位键应当被还原成真实列名。"""
        conn = FakeConnection()
        resolver = FakeResolver({("app", "users"): USERS})
        applier, dlq, _ = make_applier(conn, metas=resolver)

        ok = applier.apply_insert(
            "app", "users",
            {"UNKNOWN_COL0": 1, "UNKNOWN_COL1": "张三", "UNKNOWN_COL2": "z@x.com"},
        )

        assert ok is True
        assert dlq == [], f"不应产生失败事件: {dlq}"
        sql, params = conn.executed[0]
        assert "`id`" in sql and "`name`" in sql and "`email`" in sql
        assert "UNKNOWN_COL" not in sql, "占位键泄漏进了 SQL"
        assert params == [1, "张三", "z@x.com"]

    def test_update_maps_before_and_after_consistently(self):
        """UPDATE 的 before/after 必须用同一套映射，否则 WHERE 会错位。"""
        conn = FakeConnection()
        resolver = FakeResolver({("app", "users"): USERS})
        applier, dlq, _ = make_applier(conn, metas=resolver)

        ok = applier.apply_update(
            "app", "users",
            before={"UNKNOWN_COL0": 1, "UNKNOWN_COL1": "old", "UNKNOWN_COL2": "o@x.com"},
            after={"UNKNOWN_COL0": 1, "UNKNOWN_COL1": "new", "UNKNOWN_COL2": "n@x.com"},
        )

        assert ok is True
        sql, params = conn.executed[0]
        assert "`id` = %s" in sql.split("WHERE")[1], "WHERE 未使用主键"
        assert "UNKNOWN_COL" not in sql
        # SET 用 after 的值，WHERE 用 before 的主键
        assert params[-1] == 1

    def test_mapping_skipped_when_metadata_is_full(self):
        """binlog_row_metadata=FULL 时列名已正确，不应做任何改动。"""
        conn = FakeConnection()
        resolver = FakeResolver({("app", "users"): USERS})
        applier, dlq, _ = make_applier(conn, metas=resolver)

        ok = applier.apply_insert(
            "app", "users", {"id": 5, "name": "直接列名", "email": "d@x.com"}
        )

        assert ok is True
        sql, params = conn.executed[0]
        assert params == [5, "直接列名", "d@x.com"]


class TestDdlFailureVisibility:
    """
    DDL 失败必须可见。

    真实环境暴露的问题：目标库连接曾未指定默认库，源库不带库名限定的 DDL
    （ALTER TABLE users ADD COLUMN x）会报 "No database selected"。
    该失败原先只记一条 WARNING 就返回，不进 DLQ ——
    表现为后续大量 1054 错误，却找不到第一现场。
    """

    def test_ddl_failure_recorded_in_dlq(self):
        import mysql.connector

        class FailingCursor:
            rowcount = 0

            def execute(self, sql, params=None):
                raise mysql.connector.Error(msg="No database selected", errno=1046)

            def fetchall(self):
                return []

            def close(self):
                pass

        class FailingConn(FakeConnection):
            def cursor(self):
                return FailingCursor()

        conn = FailingConn()
        resolver = FakeResolver({("app", "users"): USERS})
        applier, dlq, _ = make_applier(conn, metas=resolver)

        ok = applier.apply_ddl("ALTER TABLE users ADD COLUMN x INT", "app")

        assert ok is False
        assert len(dlq) == 1, "DDL 失败未留下记录"
        assert dlq[0].event_type == "ddl"
        assert "No database selected" in dlq[0].error_message
        # 失败计数要递增，前端才能看到
        assert applier.stats.failed == 1

    def test_ddl_failure_payload_contains_sql(self):
        """DLQ 中要保留原始 SQL，便于人工修复后重放。"""
        import mysql.connector

        class FailingCursor:
            rowcount = 0

            def execute(self, sql, params=None):
                raise mysql.connector.Error(msg="syntax error", errno=1064)

            def fetchall(self):
                return []

            def close(self):
                pass

        class FailingConn(FakeConnection):
            def cursor(self):
                return FailingCursor()

        resolver = FakeResolver({("app", "users"): USERS})
        applier, dlq, _ = make_applier(FailingConn(), metas=resolver)

        applier.apply_ddl("ALTER TABLE bogus ADD x INT", "app")

        assert dlq[0].payload.get("sql") == "ALTER TABLE bogus ADD x INT"

    def test_ddl_success_not_recorded(self):
        """成功的 DDL 不应进 DLQ。"""
        conn = FakeConnection()
        resolver = FakeResolver({("app", "users"): USERS})
        applier, dlq, _ = make_applier(conn, metas=resolver)

        ok = applier.apply_ddl("ALTER TABLE `app`.`users` ADD COLUMN x INT", "app", "users")

        assert ok is True
        assert dlq == []
        assert applier.stats.ddl == 1


class TestDlqPayloadUsability:
    """
    DLQ 负载必须可用于重放。

    真实环境暴露的问题：表不存在时无法取得列元数据，行像保持
    UNKNOWN_COL0 这类占位键就被存入 DLQ。重放端点直接拿它构造 SQL，
    生成 `INSERT INTO t (`UNKNOWN_COL0`)`，必然失败。
    """

    def test_placeholder_keys_survive_when_table_missing(self):
        """表缺失时 DLQ 保留原始占位键（此时无从映射，属于已知限制）。"""
        conn = FakeConnection()
        resolver = FakeResolver({("app", "users"): USERS})
        applier, dlq, _ = make_applier(conn, metas=resolver)

        applier.apply_insert("app", "ghost", {"UNKNOWN_COL0": 1, "UNKNOWN_COL1": "x"})

        assert len(dlq) == 1
        assert "UNKNOWN_COL0" in dlq[0].payload["before"]

    def test_dlq_payload_has_real_columns_when_table_known(self):
        """表存在时 DLQ 必须存真实列名，否则重放必然失败。"""
        import mysql.connector

        exc = mysql.connector.Error(msg="Data too long", errno=1406)
        conn = FakeConnection(fail_on=("INSERT INTO", exc))
        resolver = FakeResolver({("app", "users"): USERS})
        applier, dlq, _ = make_applier(conn, metas=resolver)

        applier.apply_insert(
            "app", "users", {"UNKNOWN_COL0": 1, "UNKNOWN_COL1": "张三", "UNKNOWN_COL2": "z@x.com"}
        )

        assert len(dlq) == 1
        cols = set(dlq[0].payload["before"].keys())
        assert cols == {"id", "name", "email"}, f"DLQ 里是占位键: {cols}"

    def test_dlq_payload_can_be_replayed_after_mapping(self):
        """模拟重放流程：用表元数据还原列名后能成功构造 SQL。"""
        from app.services.sync.applier import ColumnMapper

        # DLQ 中的原始占位键负载
        payload_before = {"UNKNOWN_COL0": 2, "UNKNOWN_COL1": "SKU-B", "UNKNOWN_COL2": 20}

        orders = TableMeta(
            schema="app", name="orders",
            columns=("id", "sku", "qty"), pk_columns=("id",),
        )
        assert ColumnMapper.needs_mapping(payload_before)
        mapped = ColumnMapper.remap(payload_before, orders.columns)

        assert mapped == {"id": 2, "sku": "SKU-B", "qty": 20}

        result = build_insert(orders, mapped)
        assert "`id`" in result.sql and "`sku`" in result.sql
        assert "UNKNOWN_COL" not in result.sql
        assert result.params == [2, "SKU-B", 20]
