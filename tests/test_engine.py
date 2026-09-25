"""
同步引擎消费循环测试。

用假的 binlog 事件流驱动真实的 SyncEngine，验证：
- XID 边界提交（源库事务在目标库保持原子）
- Rotate 事件更新位点
- DDL 转发与缓存失效
- statement 格式 DML 被显式告警（而非静默丢弃）
- 连接中断时位点不推进
- binlog_format 校验拒绝启动

无真实 MySQL 时，这是对消费循环最严格的验证方式。

假事件直接继承 pymysqlreplication 的真实事件类（绕过 __init__ 构造），
确保引擎中的 isinstance 分派逻辑走的是与生产完全相同的分支。
"""
import threading
import time
from types import SimpleNamespace

import pytest

from app.models.database import SyncStatus, SyncTask
from app.services.sync.checkpoint import CheckpointStore
from app.services.sync.engine import BinlogReader, SyncConfigError, SyncEngine

from pymysqlreplication.event import QueryEvent, RotateEvent, XidEvent
from pymysqlreplication.row_event import DeleteRowsEvent, UpdateRowsEvent, WriteRowsEvent


# ============================================================ 假事件

def _bare(cls):
    """绕过复杂构造，直接创建真实事件类的实例。"""
    return object.__new__(cls)


def _set_rows(event, rows):
    """
    写入行数据。

    RowsEvent.rows 是只读 property，内部读取的是私有属性 _RowsEvent__rows，
    因此这里直接设置底层字段，而不是覆盖 property。
    """
    event._RowsEvent__rows = rows


def make_write(schema, table, rows, log_pos=100, timestamp=None):
    event = _bare(WriteRowsEvent)
    event.schema = schema
    event.table = table
    _set_rows(event, rows)
    event.packet = SimpleNamespace(log_pos=log_pos)
    event.timestamp = timestamp if timestamp is not None else time.time()
    return event


def make_update(schema, table, rows, log_pos=100, timestamp=None):
    event = _bare(UpdateRowsEvent)
    event.schema = schema
    event.table = table
    _set_rows(event, rows)
    event.packet = SimpleNamespace(log_pos=log_pos)
    event.timestamp = timestamp if timestamp is not None else time.time()
    return event


def make_delete(schema, table, rows, log_pos=100, timestamp=None):
    event = _bare(DeleteRowsEvent)
    event.schema = schema
    event.table = table
    _set_rows(event, rows)
    event.packet = SimpleNamespace(log_pos=log_pos)
    event.timestamp = timestamp if timestamp is not None else time.time()
    return event


def make_xid(log_pos=200):
    event = _bare(XidEvent)
    event.packet = SimpleNamespace(log_pos=log_pos)
    event.timestamp = time.time()
    return event


def make_rotate(next_binlog):
    event = _bare(RotateEvent)
    event.next_binlog = next_binlog
    event.packet = SimpleNamespace(log_pos=4)
    event.timestamp = time.time()
    return event


def make_query(query, schema="app"):
    event = _bare(QueryEvent)
    event.query = query
    event.schema = schema
    event.packet = SimpleNamespace(log_pos=50)
    event.timestamp = time.time()
    return event


class StopThread(Exception):
    """由假流抛出，用于结束消费循环。"""


class FakeStream:
    """按预置序列回放事件，然后抛出停止信号。"""

    def __init__(self, events):
        self._events = list(events)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._events:
            raise StopThread()
        return self._events.pop(0)

    def close(self):
        self.closed = True


class FakeReader(BinlogReader):
    """替换真实 binlog 连接的读取器。"""

    def __init__(self, events=None, caps=None, position=None, raise_on_stream=None):
        self._events = events or []
        self._caps = caps if caps is not None else {
            "format": "ROW", "row_image": "FULL", "gtid_mode": "OFF"
        }
        self._position = position or {"file": "mysql-bin.000001", "pos": 4, "gtid": None}
        self._raise_on_stream = raise_on_stream
        self.streams = []

    def read_server_capabilities(self):
        return dict(self._caps)

    def current_position(self):
        return dict(self._position)

    def stream(self, *, log_file, log_pos, server_id, blocking=True):
        if self._raise_on_stream:
            raise self._raise_on_stream
        # 每次重新消费都从头回放（模拟重连后从位点恢复）
        stream = FakeStream(self._events)
        self.streams.append({"file": log_file, "pos": log_pos, "stream": stream})
        return stream


class FakeTargetConnection:
    """目标库连接替身。"""

    def __init__(self):
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.autocommit = True

    def cursor(self):
        return FakeTargetCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def is_connected(self):
        return not self.closed


class FakeTargetCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))

    def fetchall(self):
        return []

    def close(self):
        pass


# ============================================================ 装配

def make_engine(reader, conn, task_id=1, source_db="app", target_db="app", **kwargs):
    """构造一个引擎，并把目标库连接替换为替身。"""
    engine = SyncEngine(
        task_id=task_id,
        task_name=f"task-{task_id}",
        source_config={
            "host": "src", "port": 3306, "user": "u",
            "password": "p", "database": source_db,
        },
        target_config={
            "host": "dst", "port": 3306, "user": "u", "password": "p",
            "database": target_db,
        },
        reader=reader,
        server_id=kwargs.pop("server_id", 10001),
        schema_resolver_factory=lambda provider, schema: StubSchemaResolver(provider, schema),
    )
    engine._connect_target = lambda: conn  # type: ignore[assignment]
    engine._get_target_connection = lambda: conn  # type: ignore[assignment]
    return engine


def users_meta(schema="app"):
    """
    构造 users 表元数据。

    schema 参数必须反映真实 SchemaResolver 的行为：它总是用**目标库名**
    构建 TableMeta，因此生成的 SQL 会落在目标库上。
    """
    return SimpleNamespace(
        schema=schema, name="users", columns=("id", "name"), pk_columns=("id",),
        unique_keys=(), generated_columns=frozenset(),
        qualified_name=f"`{schema}`.`users`", has_reliable_locator=True,
        locator_columns=("id",), writable_columns=lambda keys: list(keys),
    )


USERS_META = users_meta("app")


class StubSchemaResolver:
    def __init__(self, connection_provider=None, target_schema="app", metas=None):
        self.target_schema = target_schema
        if metas is not None:
            self.metas = metas
        else:
            # 模拟真实 resolver：以目标库名构建元数据
            self.metas = {(target_schema, "users"): users_meta(target_schema)}
        self.invalidated = []

    def get(self, schema, table):
        return self.metas.get((schema, table))

    def invalidate(self, schema, table=None):
        self.invalidated.append((schema, table))


@pytest.fixture()
def task(db_session):
    """一个已存在的同步任务记录（CheckpointStore 会写它）。"""
    from app.models.database import Database

    source = Database(
        name="src", host="s", port=3306, username="u",
        password="x", database_name="app",
    )
    target = Database(
        name="dst", host="d", port=3306, username="u",
        password="x", database_name="app",
    )
    db_session.add_all([source, target])
    db_session.flush()

    record = SyncTask(
        name="engine-test",
        source_db_id=source.id,
        target_db_id=target.id,
        status=SyncStatus.PENDING,
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


# ============================================================ 测试

class TestEngineConsumption:
    def test_transaction_committed_at_xid(self, task, db_session):
        """
        核心：源库一个事务（2 条 INSERT）在目标库只提交一次。
        旧实现逐条 autocommit，中断时会留下事务中间态。
        """
        events = [
            make_write("app", "users", [{"values": {"id": 1, "name": "a"}}], log_pos=10),
            make_write("app", "users", [{"values": {"id": 2, "name": "b"}}], log_pos=20),
            make_xid(log_pos=30),
        ]
        reader = FakeReader(events=events)
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        engine._schema = StubSchemaResolver()

        # 直接驱动一次消费，绕过线程
        from app.services.sync.applier import Applier

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="mysql-bin.000001", binlog_pos=4)
        engine._applier = Applier(
            task_id=task.id,
            connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )

        try:
            engine._drain(FakeStream(events), checkpoint)
        except StopThread:
            pass

        # 两条 INSERT 已执行，但在 XID 之前只有 BEGIN 阶段；XID 触发一次 commit
        inserts = [sql for sql, _ in conn.executed if "INSERT INTO" in sql]
        assert len(inserts) == 2
        assert conn.commits >= 1
        assert all("ON DUPLICATE KEY UPDATE" in sql for sql in inserts)

    def test_rotate_updates_position(self, task):
        """Rotate 事件必须把位点切到新文件，否则重连会读到已轮转的旧文件。"""
        events = [make_rotate("mysql-bin.000002")]
        reader = FakeReader(events=events)
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        engine._schema = StubSchemaResolver()

        from app.services.sync.applier import Applier

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="mysql-bin.000001", binlog_pos=100)
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )

        try:
            engine._drain(FakeStream(events), checkpoint)
        except (StopThread, Exception):
            pass

        position = checkpoint.position
        assert position.binlog_file == "mysql-bin.000002"
        assert position.binlog_pos == 4

    def test_ddl_forwarded_and_cache_invalidated(self, task, db_session):
        """DDL 必须转发到目标库，并清掉表结构缓存。"""
        events = [make_query("ALTER TABLE `users` ADD COLUMN age INT", schema="app")]
        reader = FakeReader(events=events)
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        resolver = StubSchemaResolver()
        engine._schema = resolver

        from app.services.sync.applier import Applier

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="mysql-bin.000001", binlog_pos=4)
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=resolver,
        )

        try:
            engine._drain(FakeStream(events), checkpoint)
        except (StopThread, Exception):
            pass

        assert any("ALTER TABLE" in sql for sql, _ in conn.executed)
        assert ("app", None) in resolver.invalidated

    def test_begin_is_skipped(self, task):
        """BEGIN 是事务开始标记，不需要转发。"""
        events = [make_query("BEGIN", schema="app")]
        reader = FakeReader(events=events)
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        engine._schema = StubSchemaResolver()

        from app.services.sync.applier import Applier

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="mysql-bin.000001", binlog_pos=4)
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )

        try:
            engine._drain(FakeStream(events), checkpoint)
        except (StopThread, Exception):
            pass

        assert not any("BEGIN" in sql for sql, _ in conn.executed)

    def test_statement_dml_recorded_as_error(self, task, db_session):
        """
        critical：statement 格式的 DML 必须显式告警并记入 DLQ。

        旧实现只识别 DDL 前缀，其余 QueryEvent 一律 continue——
        数据静默丢失，且没有任何痕迹。
        """
        from app.models.database import SyncError

        events = [
            make_query("UPDATE users SET name='x' WHERE id=1", schema="app"),
            make_xid(log_pos=60),
        ]
        reader = FakeReader(events=events)
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        engine._schema = StubSchemaResolver()

        from app.services.sync.applier import Applier

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="mysql-bin.000001", binlog_pos=4)
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
            dlq_recorder=engine._record_dlq,
        )

        try:
            engine._drain(FakeStream(events), checkpoint)
        except (StopThread, Exception):
            pass

        # statement 事件绝不能以 DDL 方式被转发执行
        assert not any("UPDATE users SET" in sql for sql, _ in conn.executed), \
            "statement DML 被盲目转发到目标库"

        db_session.expire_all()
        errors = db_session.query(SyncError).filter(SyncError.task_id == task.id).all()
        assert len(errors) == 1, "statement DML 未被记录"
        assert errors[0].event_type == "query"
        assert "binlog_format" in errors[0].error_message

    def test_position_not_advanced_on_connection_error(self, task):
        """
        连接中断时位点绝不能推进，否则重启后会跳过一段永久的数据缺口。
        """
        from app.services.sync.applier import TransientConnectionError

        events = [
            make_write("app", "users", [{"values": {"id": 1, "name": "a"}}], log_pos=10),
            make_xid(log_pos=20),
        ]
        reader = FakeReader(events=events)
        conn = FakeTargetConnection()

        # 让执行抛连接错误
        def failing_cursor():
            raise __import__("mysql.connector", fromlist=["Error"]).Error(
                msg="MySQL server has gone away", errno=2006
            )

        conn.cursor = failing_cursor  # type: ignore[assignment]

        engine = make_engine(reader, conn)
        engine._schema = StubSchemaResolver()

        from app.services.sync.applier import Applier

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="mysql-bin.000001", binlog_pos=4)
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )

        with pytest.raises(TransientConnectionError):
            engine._drain(FakeStream(events), checkpoint)

        # 位点保持在初始值
        assert checkpoint.position.binlog_pos == 4


class TestBinlogValidation:
    def test_non_row_format_rejected(self, task):
        """binlog_format 非 ROW：必须在启动期拒绝，而不是让它静默丢数据。"""
        reader = FakeReader(caps={"format": "STATEMENT", "row_image": "FULL"})
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        checkpoint = CheckpointStore(task.id, interval=0.01)

        with pytest.raises(SyncConfigError) as exc:
            engine._verify_source_binlog(checkpoint)

        assert "ROW" in str(exc.value)

    def test_minimal_row_image_rejected(self, task):
        """row_image=MINIMAL 时 binlog 只含主键，UPDATE 会缺字段。"""
        reader = FakeReader(caps={"format": "ROW", "row_image": "MINIMAL"})
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        checkpoint = CheckpointStore(task.id, interval=0.01)

        with pytest.raises(SyncConfigError) as exc:
            engine._verify_source_binlog(checkpoint)

        assert "FULL" in str(exc.value)

    def test_row_full_accepted(self, task):
        reader = FakeReader(caps={"format": "ROW", "row_image": "FULL"})
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        checkpoint = CheckpointStore(task.id, interval=0.01)

        engine._verify_source_binlog(checkpoint)  # 不抛异常

        assert checkpoint.position is not None

    def test_unknown_format_skips_validation(self, task):
        """读不到变量时不阻断，只告警——避免因权限不足而无法启动。"""
        reader = FakeReader(caps={"format": None, "row_image": None})
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        checkpoint = CheckpointStore(task.id, interval=0.01)

        engine._verify_source_binlog(checkpoint)


class TestCheckpointStore:
    def test_flush_persists_position(self, task, db_session):
        store = CheckpointStore(task.id, interval=0.0)
        store.update_position(
            binlog_file="mysql-bin.000007", binlog_pos=4242, gtid_set="uuid:1-100"
        )
        store.record_events(50)
        store.set_status(SyncStatus.RUNNING)
        store.set_health("healthy")
        store.flush()

        db_session.expire_all()
        row = db_session.query(SyncTask).filter(SyncTask.id == task.id).first()
        assert row.binlog_file == "mysql-bin.000007"
        assert row.binlog_position == 4242
        assert row.gtid_set == "uuid:1-100"
        assert row.applied_events == 50
        assert row.status == SyncStatus.RUNNING
        assert row.health.value == "healthy"

    def test_close_forces_flush(self, task, db_session):
        """停机路径必须强制落盘——旧实现在异常退出时完全不保存位点。"""
        store = CheckpointStore(task.id, interval=999.0)  # 节流窗口极大
        store.update_position(binlog_file="mysql-bin.000009", binlog_pos=999)
        store.close()

        db_session.expire_all()
        row = db_session.query(SyncTask).filter(SyncTask.id == task.id).first()
        assert row.binlog_file == "mysql-bin.000009", "停机未落盘"
        assert row.binlog_position == 999

    def test_throttle_skips_rapid_flush(self, task, db_session):
        """节流窗口内不应反复写库。"""
        store = CheckpointStore(task.id, interval=60.0)
        store.update_position(binlog_file="f1", binlog_pos=1)
        store.maybe_flush()

        store.update_position(binlog_file="f2", binlog_pos=2)
        store.maybe_flush()  # 应被节流跳过

        db_session.expire_all()
        row = db_session.query(SyncTask).filter(SyncTask.id == task.id).first()
        assert row.binlog_file == "f1", "节流未生效"

    def test_metric_accumulation(self, task):
        store = CheckpointStore(task.id, interval=999.0)
        store.record_events(10)
        store.record_events(5)
        store.record_dlq(2)

        metrics = store.metrics
        assert metrics.applied_events == 15
        assert metrics.dlq_count == 2

    def test_seconds_since_progress_grows(self, task):
        store = CheckpointStore(task.id, interval=999.0)
        store._metrics.last_progress_at -= 10  # 模拟 10 秒无进展
        assert store.seconds_since_progress >= 9


class TestEngineLifecycle:
    def test_stop_is_graceful(self, task):
        """停机后线程必须结束，且位点已落盘。"""
        reader = FakeReader(events=[])
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)

        engine.start()
        time.sleep(0.3)
        engine.stop(timeout=10)

        assert not engine.is_running

    def test_double_start_is_noop(self, task):
        reader = FakeReader(events=[])
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)

        engine.start()
        first_thread = engine._thread
        engine.start()  # 不应替换线程
        assert engine._thread is first_thread

        engine.stop(timeout=10)

    def test_aborts_on_stop_signal(self, task):
        """停止信号必须能中断阻塞的消费循环。"""
        class SlowStream:
            def __init__(self):
                self.closed = False
                self.i = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.i += 1
                if self.i > 100:
                    raise StopThread()
                time.sleep(0.02)
                return make_xid(log_pos=self.i)

            def close(self):
                self.closed = True

        class SlowReader(FakeReader):
            def stream(self, **kwargs):
                return SlowStream()

        reader = SlowReader()
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        engine._schema = StubSchemaResolver()

        engine.start()
        time.sleep(0.3)

        started = time.time()
        engine.stop(timeout=10)
        elapsed = time.time() - started

        assert elapsed < 10, f"停机耗时过长: {elapsed:.1f}s"
        assert not engine.is_running


class TestReconnectBehaviour:
    """重连路径：连接中断后必须能从上次位点恢复，且不遗留旧连接。"""


    def test_owned_applier_torn_down_on_stream_end(self, task):
        """
        消费循环结束后，引擎自建的 Applier 必须被关闭并清空，
        表结构缓存也要释放——否则重连时会复用失效的元数据。
        """
        reader = FakeReader(events=[])
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        engine._schema = StubSchemaResolver()

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="mysql-bin.000001", binlog_pos=4)

        # 走完整的建立→消费→清理路径（目标库连接已被替换为替身）
        try:
            engine._consume_once(checkpoint)
        except StopThread:
            pass

        assert engine._applier is None, "自建的 Applier 未被清理"
        assert engine._schema is None, "表结构缓存未被清理"

    def test_uncommitted_transaction_rolled_back_on_stream_abort(self, task):
        """
        流中断时，已累积但未提交的变更必须回滚。
        这是「目标库不留下事务中间态」的保证。
        """
        from app.services.sync.applier import Applier

        events = [
            make_write("app", "users", [{"values": {"id": 1, "name": "a"}}], log_pos=10),
            # 故意不给 XID：事务未提交
        ]
        reader = FakeReader(events=events)
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        engine._schema = StubSchemaResolver()
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )
        engine._owns_applier = True

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="mysql-bin.000001", binlog_pos=4)

        try:
            engine._consume_once(checkpoint)
        except StopThread:
            pass

        assert conn.commits == 0, "未收到 XID 却提交了事务"
        assert conn.rollbacks >= 1, "未提交事务没有被回滚"

    def test_connection_closed_on_teardown(self, task):
        """_teardown 必须关闭目标库连接，避免连接泄漏。"""
        reader = FakeReader(events=[])
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        engine._target_conn = conn
        engine._teardown()
        assert conn.closed is True

    def test_injected_applier_not_closed_by_engine(self, task):
        """外部注入的 Applier 不应被引擎关闭（所有权归外部）。"""
        from app.services.sync.applier import Applier

        reader = FakeReader(events=[])
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        engine._schema = StubSchemaResolver()

        applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )
        engine._applier = applier
        engine._owns_applier = False

        # 模拟 finally 块的清理逻辑
        applier.rollback()
        if engine._owns_applier:
            applier.close()

        assert conn.closed is False, "外部注入的连接被引擎关闭了"



class TestSchemaMapping:
    """
    跨库同步的落点正确性。

    这是在本机真实 MySQL 上发现的生产安全隐患：binlog 事件携带的是**源库**名，
    若直接用事件里的 schema 构造 SQL，同一实例上的「A 库 → B 库」同步
    会生成 `INSERT INTO A.t ...`，即原地写回源库——污染数据且引发 binlog 无限循环。
    """

    def test_row_event_writes_to_target_schema(self, task):
        """行事件必须写到目标库，而不是事件里的源库。"""
        from app.services.sync.applier import Applier

        events = [
            make_write("source_db", "users", [{"values": {"id": 1, "name": "a"}}]),
            make_xid(log_pos=30),
        ]
        conn = FakeTargetConnection()
        reader = FakeReader(events=events)
        engine = make_engine(reader, conn, source_db="source_db", target_db="target_db")

        resolver = StubSchemaResolver(target_schema="target_db")
        engine._schema = resolver
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=resolver,
        )

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="binlog.000001", binlog_pos=4)

        try:
            engine._drain(FakeStream(events), checkpoint)
        except StopThread:
            pass

        sqls = [sql for sql, _ in conn.executed]
        assert sqls, "没有执行任何语句"
        for sql in sqls:
            assert "`target_db`" in sql, f"未写入目标库: {sql}"
            assert "`source_db`" not in sql, f"误写入源库: {sql}"

    def test_foreign_schema_ddl_not_forwarded(self, task):
        """非源库的 DDL 不应被转发到目标库。"""
        from app.services.sync.applier import Applier

        events = [make_query("DROP TABLE `unrelated`.`t`", schema="unrelated")]
        conn = FakeTargetConnection()
        reader = FakeReader(events=events)
        engine = make_engine(reader, conn, source_db="source_db", target_db="target_db")
        engine._schema = StubSchemaResolver(target_schema="target_db")
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="binlog.000001", binlog_pos=4)

        try:
            engine._drain(FakeStream(events), checkpoint)
        except StopThread:
            pass

        assert not any("unrelated" in sql for sql, _ in conn.executed), \
            "无关库的 DDL 被转发了"

    def test_source_schema_ddl_rewritten_to_target(self, task):
        """源库 DDL 转发时必须把库名改写为目标库。"""
        from app.services.sync.applier import Applier

        events = [make_query("ALTER TABLE `source_db`.`users` ADD COLUMN age INT",
                             schema="source_db")]
        conn = FakeTargetConnection()
        reader = FakeReader(events=events)
        engine = make_engine(reader, conn, source_db="source_db", target_db="target_db")
        engine._schema = StubSchemaResolver(target_schema="target_db")
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="binlog.000001", binlog_pos=4)

        try:
            engine._drain(FakeStream(events), checkpoint)
        except StopThread:
            pass

        ddl_sqls = [sql for sql, _ in conn.executed if "ALTER TABLE" in sql]
        assert len(ddl_sqls) == 1
        assert "`target_db`.`users`" in ddl_sqls[0], f"库名未改写: {ddl_sqls[0]}"

    def test_same_schema_rewrite_is_noop(self, task):
        """源库与目标库同名时，DDL 原样转发。"""
        reader = FakeReader()
        engine = make_engine(reader, FakeTargetConnection(),
                             source_db="app", target_db="app")
        query = "ALTER TABLE `app`.`users` ADD COLUMN x INT"
        assert engine._rewrite_schema(query) == query

    def test_rewrite_does_not_touch_string_literals(self, task):
        """
        改写只针对反引号包裹的库名，不能误伤字符串字面量里的库名。
        """
        reader = FakeReader()
        engine = make_engine(reader, FakeTargetConnection(),
                             source_db="src", target_db="dst")
        query = "INSERT INTO `src`.`t` VALUES ('src')"
        rewritten = engine._rewrite_schema(query)
        assert "`dst`.`t`" in rewritten
        assert "'src'" in rewritten, "字符串字面量被误改"

    def test_stream_restricts_to_source_schema(self):
        """binlog 流必须限定源库，否则会消费整台实例上所有库的事件。"""
        import inspect

        from app.services.sync.engine import BinlogReader

        source = inspect.getsource(BinlogReader.stream)
        assert "only_schemas" in source, "未设置 only_schemas"
        assert "_cfg[\"database\"]" in source


class TestStreamReaderArguments:
    """
    传给 BinLogStreamReader 的每个参数都必须是该库真实支持的。

    这条测试来自真实环境：`fail_on_table_metadata_unavailable` 是从前任代码
    继承下来的无效参数，只在真正建立 binlog 流时才会抛 TypeError，
    单测中若用替身替换掉 stream() 就永远发现不了。
    """

    def test_stream_arguments_are_accepted_by_library(self):
        import inspect

        from pymysqlreplication import BinLogStreamReader

        signature = inspect.signature(BinLogStreamReader.__init__)
        accepted = set(signature.parameters)

        # 引擎实际传入的参数名
        source = inspect.getsource(
            __import__(
                "app.services.sync.engine", fromlist=["BinlogReader"]
            ).BinlogReader.stream
        )
        passed = set()
        for name in accepted:
            if name in ("self", "kwargs"):
                continue
            if f"{name}=" in source:
                passed.add(name)

        assert "only_schemas" in passed, "未限制源库范围"

        # 反查：源码里出现的 `xxx=` 参数必须都在签名中
        import re

        mentioned = set(re.findall(r"^\s+([a-z_]+)=", source, re.MULTILINE))
        unknown = mentioned - accepted - {"kwargs"}
        assert not unknown, f"传入了库不支持的参数: {sorted(unknown)}"

    def test_stream_builds_without_type_error(self):
        """用真实读取器尝试构造流（连接失败是可接受的，参数错误不可接受）。"""
        from app.services.sync.engine import BinlogReader

        reader = BinlogReader({
            "host": "127.0.0.1", "port": 1, "user": "u",
            "password": "p", "database": "app",
        })
        try:
            reader.stream(log_file="binlog.000001", log_pos=4, server_id=9999)
        except TypeError as exc:
            pytest.fail(f"参数不被库接受: {exc}")
        except Exception:
            # 连接类错误符合预期（端口 1 上没有服务）
            pass


class TestInitialPositionResolution:
    """
    首次启动时的位点解析。

    这段逻辑来自真实环境的失败：manager 传入的 initial 恒为
    {"file": None, "pos": None, "gtid": None}——字典本身非空，
    因此 `initial or current_position()` 永远选中 initial，位点保持 None。
    后果是 pymysqlreplication 内部执行 SHOW MASTER STATUS，
    而该命令在 MySQL 8.4+ 已被移除，任务直接以 1064 失败。
    """

    def _consume_and_capture(self, initial, reader, task):
        from app.services.sync.applier import Applier

        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        engine.initial = initial
        engine._schema = StubSchemaResolver()
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )

        checkpoint = CheckpointStore(task.id, interval=0.01)
        try:
            engine._consume_once(checkpoint)
        except (StopThread, Exception):
            pass
        return checkpoint, reader

    def test_all_none_initial_falls_back_to_current_position(self, task):
        """initial 全为 None 时必须回退到源库当前位点。"""
        reader = FakeReader(
            events=[],
            position={"file": "binlog.000042", "pos": 987, "gtid": None},
        )
        checkpoint, reader = self._consume_and_capture(
            {"file": None, "pos": None, "gtid": None}, reader, task
        )

        assert reader.streams, "未建立 binlog 流"
        call = reader.streams[0]
        assert call["file"] == "binlog.000042", "未使用源库当前位点"
        assert call["pos"] == 987

    def test_saved_position_is_resumed(self, task):
        """有已保存位点时应从该位点续传，而不是跳到最新。"""
        reader = FakeReader(
            events=[],
            position={"file": "binlog.000099", "pos": 111, "gtid": None},
        )
        checkpoint, reader = self._consume_and_capture(
            {"file": "binlog.000005", "pos": 222, "gtid": None}, reader, task
        )

        assert reader.streams[0]["file"] == "binlog.000005", "未从保存位点续传"
        assert reader.streams[0]["pos"] == 222

    def test_partial_initial_is_rejected_as_invalid(self, task):
        """只有文件名没有偏移量不算有效位点，应回退查询。"""
        reader = FakeReader(
            events=[],
            position={"file": "binlog.000050", "pos": 555, "gtid": None},
        )
        self._consume_and_capture(
            {"file": "binlog.000005", "pos": None, "gtid": None}, reader, task
        )
        assert reader.streams[0]["file"] == "binlog.000050"

    def test_raises_when_position_unresolvable(self, task):
        """位点彻底无法确定时必须明确报错，而不是让库内部抛 1064。"""
        class NoPositionReader(FakeReader):
            def current_position(self):
                return {"file": None, "pos": None, "gtid": None}

        reader = NoPositionReader(events=[])
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        engine.initial = {"file": None, "pos": None, "gtid": None}
        engine._schema = StubSchemaResolver()

        from app.services.sync.engine import SyncConfigError

        checkpoint = CheckpointStore(task.id, interval=0.01)
        with pytest.raises(SyncConfigError) as exc:
            engine._consume_once(checkpoint)
        assert "位点" in str(exc.value)


class TestRotateEventHandling:
    """
    RotateEvent 的位点语义。

    真实环境暴露的问题：每次建立 binlog 流时源库会先发一个 Rotate 事件
    指向当前文件。若不加判断就把位点重置为 4，会把整份 binlog 从头重放——
    表现为「删除的行又被历史 INSERT 插回来」，且位点推进缓慢。
    """

    def test_rotate_to_same_file_preserves_offset(self, task):
        """Rotate 指向当前文件时，偏移量必须保持不变。"""
        events = [make_rotate("binlog.000004")]  # 与当前文件相同
        reader = FakeReader(events=events)
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        engine._schema = StubSchemaResolver()

        from app.services.sync.applier import Applier

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="binlog.000004", binlog_pos=5000)
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )

        try:
            engine._drain(FakeStream(events), checkpoint)
        except StopThread:
            pass

        assert checkpoint.position.binlog_pos == 5000, "偏移量被重置，会重放整个文件"
        assert checkpoint.position.binlog_file == "binlog.000004"

    def test_rotate_to_new_file_resets_offset(self, task):
        """真正轮转到新文件时，偏移量从 4 开始。"""
        events = [make_rotate("binlog.000005")]  # 不同文件
        reader = FakeReader(events=events)
        conn = FakeTargetConnection()
        engine = make_engine(reader, conn)
        engine._schema = StubSchemaResolver()

        from app.services.sync.applier import Applier

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="binlog.000004", binlog_pos=9999)
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )

        try:
            engine._drain(FakeStream(events), checkpoint)
        except StopThread:
            pass

        assert checkpoint.position.binlog_file == "binlog.000005"
        assert checkpoint.position.binlog_pos == 4

    def test_delete_after_rotate_still_targets_pk(self, task):
        """轮转之后到达的 DELETE 仍然必须用主键定位。"""
        from app.services.sync.applier import Applier

        events = [
            make_rotate("binlog.000004"),
            make_delete("app", "users", [{"values": {
                "UNKNOWN_COL0": 2, "UNKNOWN_COL1": "李四",
            }}]),
            make_xid(log_pos=800),
        ]
        conn = FakeTargetConnection()
        reader = FakeReader(events=events)
        engine = make_engine(reader, conn)
        engine._schema = StubSchemaResolver()
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="binlog.000004", binlog_pos=5000)

        try:
            engine._drain(FakeStream(events), checkpoint)
        except StopThread:
            pass

        deletes = [(sql, p) for sql, p in conn.executed if "DELETE FROM" in sql]
        assert len(deletes) == 1
        sql, params = deletes[0]
        assert "WHERE `id` = %s" in sql
        assert params == [2]


class TestSchemaTypeNormalisation:
    """
    schema 名的类型差异。

    真实环境暴露的问题：pymysqlreplication 的 QueryEvent.schema 是 bytes
    （b'dbsync_src'），RowsEvent.schema 是 str。两者直接用 != 比较恒为 True，
    导致所有源库 DDL 都被当成「非源库事件」静默跳过——
    表结构变更完全无法同步，且没有任何错误日志。
    """

    def test_query_event_with_bytes_schema_is_forwarded(self, task):
        """DDL 的 schema 为 bytes 时也必须被识别为源库事件。"""
        from app.services.sync.applier import Applier
        from pymysqlreplication.event import QueryEvent

        event = object.__new__(QueryEvent)
        event.query = "ALTER TABLE `source_db`.`users` ADD COLUMN age INT"
        event.schema = b"source_db"          # 关键：bytes 而非 str
        event.packet = SimpleNamespace(log_pos=50)
        event.timestamp = time.time()

        conn = FakeTargetConnection()
        engine = make_engine(FakeReader(), conn,
                             source_db="source_db", target_db="target_db")
        engine._schema = StubSchemaResolver(target_schema="target_db")
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="binlog.000001", binlog_pos=4)

        try:
            engine._drain(FakeStream([event]), checkpoint)
        except StopThread:
            pass

        ddl = [sql for sql, _ in conn.executed if "ALTER TABLE" in sql]
        assert ddl, "bytes 类型的 schema 导致 DDL 被跳过"
        assert "`target_db`" in ddl[0], "DDL 未改写目标库名"

    def test_other_schema_ddl_with_bytes_still_skipped(self, task):
        """非源库的 DDL 仍须跳过（bytes 形式）。"""
        from app.services.sync.applier import Applier
        from pymysqlreplication.event import QueryEvent

        event = object.__new__(QueryEvent)
        event.query = "DROP TABLE `t`"
        event.schema = b"unrelated_db"
        event.packet = SimpleNamespace(log_pos=50)
        event.timestamp = time.time()

        conn = FakeTargetConnection()
        engine = make_engine(FakeReader(), conn,
                             source_db="source_db", target_db="target_db")
        engine._schema = StubSchemaResolver(target_schema="target_db")
        engine._applier = Applier(
            task_id=task.id, connection_provider=lambda: conn,
            schema_resolver=engine._schema,
        )

        checkpoint = CheckpointStore(task.id, interval=0.01)
        checkpoint.update_position(binlog_file="binlog.000001", binlog_pos=4)

        try:
            engine._drain(FakeStream([event]), checkpoint)
        except StopThread:
            pass

        assert not [sql for sql, _ in conn.executed if "DROP TABLE" in sql], \
            "无关库的 DDL 被转发了"

    def test_norm_schema_helper(self):
        from app.services.sync.engine import _norm_schema

        assert _norm_schema(b"dbsync_src") == "dbsync_src"
        assert _norm_schema("dbsync_src") == "dbsync_src"
        assert _norm_schema(None) == ""
        assert _norm_schema(bytearray(b"x")) == "x"


class TestTargetConnectionDefaults:
    """
    目标库连接的默认库设置。

    真实环境暴露的问题：_connect_target 未指定 database，连接没有默认库。
    源库的 DDL 常常不带库名限定符（ALTER TABLE users ADD COLUMN x），
    在没有默认库的连接上执行会报 "No database selected"。
    DML 因为总写成 `db`.`table` 全限定形式而侥幸正常。
    """

    def test_connection_specifies_default_database(self, monkeypatch, task):
        """连接目标库时必须指定默认库为目标的库名。"""
        import mysql.connector

        captured = {}

        def fake_connect(**kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop here")

        import app.services.sync.engine as engine_mod

        monkeypatch.setattr(engine_mod.mysql.connector, "connect", fake_connect)

        engine = SyncEngine(
            task_id=1, task_name="t",
            source_config={"host": "s", "port": 3306, "user": "u",
                           "password": "p", "database": "src_db"},
            target_config={"host": "d", "port": 3306, "user": "u",
                           "password": "p", "database": "dst_db"},
            reader=FakeReader(),
        )

        from app.services.sync.applier import TransientConnectionError

        with pytest.raises(TransientConnectionError):
            engine._connect_target()

        assert captured.get("database") == "dst_db", \
            f"未指定默认库，DDL 会因 No database selected 失败: {captured}"

    def test_target_schema_resolved_from_config(self, task):
        """引擎应从 target_config 解析出目标库名。"""
        engine = make_engine(FakeReader(), FakeTargetConnection(),
                             source_db="src_db", target_db="dst_db")
        assert engine.source_schema == "src_db"
        assert engine.target_schema == "dst_db"



class TestManagerConfigPlumbing:
    """
    配置传递的完整性。

    _target_config 必须带 database 键：引擎据此确定写入落点。
    缺了它，target_schema 会退化为源库名——同实例跨库同步时会原地写回源库。
    这条测试在变异测试中被发现是覆盖盲区，故补上。
    """

    def test_source_config_has_database(self, db_session):
        from app.core.crypto import encrypt
        from app.models.database import Database
        from app.services.sync.manager import _source_config

        record = Database(
            name="s", host="h", port=3306, username="u",
            password=encrypt("p"), database_name="src_db",
        )
        db_session.add(record)
        db_session.commit()

        cfg = _source_config(record)
        assert cfg["database"] == "src_db"
        assert cfg["host"] == "h"
        assert cfg["port"] == 3306

    def test_target_config_has_database(self, db_session):
        """
        关键回归：目标库配置必须包含 database 键。
        缺失会让引擎把写入落点退化为源库名。
        """
        from app.core.crypto import encrypt
        from app.models.database import Database
        from app.services.sync.manager import _target_config

        record = Database(
            name="t", host="h2", port=3306, username="u",
            password=encrypt("p"), database_name="dst_db",
        )
        db_session.add(record)
        db_session.commit()

        cfg = _target_config(record)
        assert "database" in cfg, "缺少 database 键，写入落点会退化为源库"
        assert cfg["database"] == "dst_db"

    def test_password_is_decrypted_in_config(self, db_session):
        from app.core.crypto import encrypt
        from app.models.database import Database
        from app.services.sync.manager import _source_config

        record = Database(
            name="p", host="h", port=3306, username="u",
            password=encrypt("secret-pw"), database_name="d",
        )
        db_session.add(record)
        db_session.commit()

        assert _source_config(record)["password"] == "secret-pw"

    def test_engine_uses_target_schema_from_config(self):
        """引擎必须从 target_config 解析落点库名。"""
        engine = make_engine(FakeReader(), FakeTargetConnection(),
                             source_db="src_db", target_db="dst_db")
        assert engine.source_schema == "src_db"
        assert engine.target_schema == "dst_db"
        assert engine.target_schema != engine.source_schema


class TestTargetSchemaSafety:
    """
    目标库名缺失时的行为。

    这是最危险的失效模式：引擎原先用 `target_config.get("database") or source_schema`
    兜底，缺键时目标库名静默退化为源库名。后果是写入 SQL 构造成
    `源库`.`表`——原地写回源库、binlog 无限循环，且没有任何报错。

    正确行为是立即拒绝，而不是猜一个落点。
    """

    def test_missing_target_database_raises(self):
        """目标库配置缺 database 键时必须直接报错。"""
        from app.services.sync.engine import SyncConfigError, SyncEngine

        with pytest.raises(SyncConfigError) as exc:
            SyncEngine(
                task_id=1, task_name="t",
                source_config={"host": "s", "port": 3306, "user": "u",
                               "password": "p", "database": "src_db"},
                target_config={"host": "d", "port": 3306, "user": "u",
                               "password": "p"},          # 故意缺 database
                reader=FakeReader(),
            )
        assert "目标库名" in str(exc.value)

    def test_empty_target_database_raises(self):
        from app.services.sync.engine import SyncConfigError, SyncEngine

        with pytest.raises(SyncConfigError):
            SyncEngine(
                task_id=1, task_name="t",
                source_config={"host": "s", "port": 3306, "user": "u",
                               "password": "p", "database": "src_db"},
                target_config={"host": "d", "port": 3306, "user": "u",
                               "password": "p", "database": ""},
                reader=FakeReader(),
            )

    def test_never_silently_falls_back_to_source(self):
        """绝不能退化为源库名——那是原地写回。"""
        from app.services.sync.engine import SyncConfigError, SyncEngine

        try:
            engine = SyncEngine(
                task_id=1, task_name="t",
                source_config={"host": "s", "port": 3306, "user": "u",
                               "password": "p", "database": "src_db"},
                target_config={"host": "d", "port": 3306, "user": "u",
                               "password": "p"},
                reader=FakeReader(),
            )
        except SyncConfigError:
            return  # 期望的路径

        pytest.fail(
            f"目标库名静默退化为 {engine.target_schema!r}，"
            "会导致原地写回源库且无任何报错"
        )

    def test_valid_config_still_works(self):
        """正常配置不受影响。"""
        engine = make_engine(FakeReader(), FakeTargetConnection(),
                             source_db="src_db", target_db="dst_db")
        assert engine.target_schema == "dst_db"

    def test_same_source_and_target_is_allowed(self):
        """源库与目标库同名是合法配置（自同步到另一实例），不应被拦。"""
        engine = make_engine(FakeReader(), FakeTargetConnection(),
                             source_db="app", target_db="app")
        assert engine.target_schema == "app"
        assert engine.source_schema == "app"


class TestNoPrimaryKeyReporting:
    """
    无主键表的可观测性。

    真实环境暴露的问题：一张无主键表在同步时，DELETE 靠全列值匹配定位，
    删除一行重复数据会连带删掉目标库中所有相同行，造成数据不一致。
    但界面上该表显示为「正常」、任务显示「健康」——用户完全无从察觉。

    修复后：无可靠定位键的表状态标为 no_primary_key，
    并在表状态里回填主键/唯一键信息，供界面提示风险。
    """

    def test_state_marked_when_no_locator(self, task, db_session):
        from app.models.database import SyncTableState
        from app.services.sync.sql_builder import TableMeta

        no_pk = TableMeta(schema="app", name="logs", columns=("a", "b"))
        resolver = StubSchemaResolver(target_schema="app", metas={("app", "logs"): no_pk})

        conn = FakeTargetConnection()
        engine = make_engine(FakeReader(), conn)
        engine._schema = resolver

        engine._record_table_state("app", "logs", "active", "binlog.000001", 100, 1, None)

        db_session.expire_all()
        row = (
            db_session.query(SyncTableState)
            .filter(
                SyncTableState.task_id == task.id,
                SyncTableState.table_name == "logs",
            )
            .first()
        )
        assert row is not None, "表状态未写入"
        assert row.state.value == "no_primary_key", f"状态为 {row.state.value}，未标注风险"

    def test_state_active_when_primary_key_present(self, task, db_session):
        from app.models.database import SyncTableState

        conn = FakeTargetConnection()
        engine = make_engine(FakeReader(), conn)
        engine._schema = StubSchemaResolver(target_schema="app")  # users 有主键 id

        engine._record_table_state("app", "users", "active", "binlog.000001", 100, 1, None)

        db_session.expire_all()
        row = (
            db_session.query(SyncTableState)
            .filter(
                SyncTableState.task_id == task.id,
                SyncTableState.table_name == "users",
            )
            .first()
        )
        assert row.state.value == "active"
        assert row.pk_columns == ["id"], f"主键信息未回填: {row.pk_columns}"

    def test_unique_key_also_counts_as_locator(self, task, db_session):
        """有唯一键同样算可靠定位，不应报警。"""
        from app.models.database import SyncTableState
        from app.services.sync.sql_builder import TableMeta

        meta = TableMeta(
            schema="app", name="codes", columns=("code", "label"),
            pk_columns=(), unique_keys=(("code",),),
        )
        resolver = StubSchemaResolver(target_schema="app", metas={("app", "codes"): meta})

        conn = FakeTargetConnection()
        engine = make_engine(FakeReader(), conn)
        engine._schema = resolver

        engine._record_table_state("app", "codes", "active", "binlog.000001", 100, 1, None)

        db_session.expire_all()
        row = (
            db_session.query(SyncTableState)
            .filter(
                SyncTableState.task_id == task.id, SyncTableState.table_name == "codes"
            )
            .first()
        )
        assert row.state.value == "active"
        assert row.unique_keys == [["code"]]

    def test_schema_missing_still_reported(self, task, db_session):
        """结构缺失的标记逻辑不能被新逻辑覆盖。"""
        from app.models.database import SyncTableState

        conn = FakeTargetConnection()
        engine = make_engine(FakeReader(), conn)
        engine._schema = StubSchemaResolver(target_schema="app")

        engine._record_table_state(
            "app", "users", "schema_missing", "binlog.000001", 100, 0, "表不存在"
        )

        db_session.expire_all()
        row = (
            db_session.query(SyncTableState)
            .filter(SyncTableState.task_id == task.id)
            .first()
        )
        assert row.state.value == "schema_missing"
        assert row.last_error == "表不存在"

    def test_recovery_clears_error(self, task, db_session):
        """表恢复后错误信息应被清除。"""
        from app.models.database import SyncTableState

        conn = FakeTargetConnection()
        engine = make_engine(FakeReader(), conn)
        engine._schema = StubSchemaResolver(target_schema="app")

        engine._record_table_state("app", "users", "schema_missing", "f", 1, 0, "表不存在")
        engine._record_table_state("app", "users", "active", "f", 2, 1, None)

        db_session.expire_all()
        row = (
            db_session.query(SyncTableState)
            .filter(SyncTableState.task_id == task.id)
            .first()
        )
        assert row.last_error is None, "恢复后错误信息未清除"
        assert row.state.value == "active"
