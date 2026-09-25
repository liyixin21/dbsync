"""
行事件的应用器。

职责：把 binlog 行事件翻译成目标库写入，并保证：

1. **事务边界**——以 binlog 的 XID 事件为提交点。源库一个事务在目标库原子落地，
   不再逐条 autocommit 导致中间态。
2. **幂等重放**——INSERT 带 ON DUPLICATE KEY UPDATE，崩溃后从位点重放无副作用。
3. **错误分级**——不同错误类别走不同处置，绝不再出现「一次主键冲突 = 整表永久停摆」。
4. **可观测**——失败事件进入 DLQ，表状态实时更新，前端的「运行中」不再掩盖问题。
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from loguru import logger

from . import errors as err_mod
from .errors import ErrorClass
from .sql_builder import TableMeta, build_delete, build_insert, build_update


def _stringify(values: Dict[Any, Any]) -> Dict[str, Any]:
    """键值都转成可 JSON 序列化的形式，用于 DLQ 记录。"""
    return {str(k): v for k, v in values.items()}


def _extract_index(key: Any) -> int:
    """
    从 binlog 行像的键中提取列序号。

    当源库 binlog_row_metadata=MINIMAL 时，binlog 不携带列名，
    pymysqlreplication 用 "UNKNOWN_COL0"、"UNKNOWN_COL1" 之类的占位键，
    或直接使用整数下标。这里统一还原成序号，以便按顺序映射到真实列名。
    """
    if isinstance(key, int):
        return key
    text = str(key)
    for prefix in ("UNKNOWN_COL", "UNKNOWN_"):
        if text.startswith(prefix):
            suffix = text[len(prefix):]
            if suffix.isdigit():
                return int(suffix)
    try:
        return int(text)
    except ValueError:
        return 0


class ColumnMapper:
    """
    把 binlog 行像的键映射为真实列名。

    背景：源库 binlog_row_metadata=MINIMAL（MySQL 8.0 默认值）时，
    binlog 事件只记录列的数量与顺序，不记录列名。此时库里给出的是
    UNKNOWN_COL0 这类占位键，直接拿去构造 SQL 会报 1054 Unknown column。

    处理策略：按 ORDINAL_POSITION 顺序与表定义的真实列名对齐。
    这是唯一可行的做法——顺序信息在 binlog 中是可靠的。
    """

    @staticmethod
    def needs_mapping(values: Dict[str, Any]) -> bool:
        """行像是否使用占位键或整数下标。"""
        if not values:
            return False
        for key in values:
            if isinstance(key, int):
                return True
            text = str(key)
            if text.startswith("UNKNOWN_COL") or text.startswith("UNKNOWN_"):
                return True
            if text.isdigit():
                return True
        return False

    @staticmethod
    def remap(values: Dict[str, Any], columns: Sequence[str]) -> Dict[str, Any]:
        """
        按列顺序重映射为真实列名。

        无法对齐的列（数量超出表定义）保留占位名，由后续环节记录为错误，
        不静默丢弃。
        """
        if not values:
            return {}

        ordered = sorted(values.keys(), key=_extract_index)
        mapped: Dict[str, Any] = {}
        overflow: Dict[str, Any] = {}

        for index, key in enumerate(ordered):
            if index < len(columns):
                mapped[columns[index]] = values[key]
            else:
                overflow[f"__overflow_{index}"] = values[key]

        return {**mapped, **overflow}


class TransientConnectionError(Exception):
    """
    连接类错误。向上抛出以触发重连，并从最后已提交位点恢复。

    位点绝不因此推进——否则会产生永久性数据缺口。
    """


@dataclass
class ApplyStats:
    """单次事务的统计。"""

    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    ddl: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.deleted

    def merge(self, other: "ApplyStats") -> None:
        self.inserted += other.inserted
        self.updated += other.updated
        self.deleted += other.deleted
        self.skipped += other.skipped
        self.failed += other.failed
        self.ddl += other.ddl

    def reset(self) -> None:
        self.inserted = self.updated = self.deleted = 0
        self.skipped = self.failed = self.ddl = 0


@dataclass
class FailedEvent:
    """一条进入 DLQ 的失败事件。"""

    schema: str
    table: str
    event_type: str
    error_class: ErrorClass
    error_code: Optional[int]
    error_message: str
    payload: Dict[str, Any] = field(default_factory=dict)
    binlog_file: Optional[str] = None
    binlog_pos: Optional[int] = None


# 回调签名
DlqRecorder = Callable[[FailedEvent], None]
TableStateRecorder = Callable[[str, str, str, Optional[str], Optional[List[str]], int, Optional[str]], None]


class Applier:
    """
    目标库应用器。

    非线程安全：一个实例只属于一个同步任务的消费线程。
    """

    def __init__(
        self,
        *,
        task_id: int,
        connection_provider: Callable[[], Any],
        schema_resolver,
        dlq_recorder: Optional[DlqRecorder] = None,
        table_state_recorder: Optional[TableStateRecorder] = None,
        tx_max_rows: int = 10000,
    ) -> None:
        self.task_id = task_id
        self._connect = connection_provider
        self._schema = schema_resolver
        self._dlq = dlq_recorder
        self._table_state = table_state_recorder
        self._tx_max_rows = tx_max_rows

        self._conn = None
        self._pending_rows = 0
        self._in_tx = False
        # 本次事务内已汇报过 schema 缺失的表，避免同一事务重复刷 DLQ
        self._reported: set = set()
        # 已提示过列名映射的表，只提示一次
        self._mapped_notified: set = set()
        self.stats = ApplyStats()
        self.pending_errors: List[FailedEvent] = []

    # ------------------------------------------------------------ 连接

    def _connection(self):
        if self._conn is None:
            self._conn = self._connect()
            # 关闭 autocommit，由 XID 边界显式提交
            try:
                self._conn.autocommit = False
            except Exception:  # pragma: no cover - 驱动差异
                pass
        return self._conn

    def close(self) -> None:
        """回滚未提交事务并断开连接。"""
        if self._conn is not None:
            if self._in_tx:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                self._in_tx = False
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _begin_if_needed(self) -> None:
        if not self._in_tx:
            self._connection()
            self._in_tx = True
            self._pending_rows = 0
            self._reported.clear()

    # ------------------------------------------------------------ 事务边界

    def commit(self) -> ApplyStats:
        """
        XID 事件到达：提交当前事务。

        返回本次提交的事务统计（调用方用于累计与位点推进）。
        """
        stats = self.stats
        if self._in_tx and self._conn is not None:
            try:
                self._conn.commit()
            except Exception as exc:
                cls, _ = err_mod.classify_error(exc)
                self._in_tx = False
                self._conn = None
                if not err_mod.should_advance_checkpoint(cls):
                    raise TransientConnectionError(f"提交事务失败，将从上次位点恢复: {exc}") from exc
                raise
            finally:
                pass
        self._in_tx = False
        self._pending_rows = 0
        self._reported.clear()
        self.pending_errors = []
        self.stats = ApplyStats()
        return stats

    def rollback(self) -> None:
        """回滚当前事务。"""
        if self._in_tx and self._conn is not None:
            try:
                self._conn.rollback()
            except Exception:
                pass
        self._in_tx = False
        self._pending_rows = 0
        self._reported.clear()
        self.pending_errors = []
        self.stats = ApplyStats()

    # ------------------------------------------------------------ 行事件

    def apply_insert(self, schema: str, table: str, row: Dict[str, Any],
                     binlog_file: Optional[str] = None,
                     binlog_pos: Optional[int] = None) -> bool:
        return self._apply(schema, table, "insert", row, None, binlog_file, binlog_pos)

    def apply_update(self, schema: str, table: str, before: Dict[str, Any],
                     after: Dict[str, Any],
                     binlog_file: Optional[str] = None,
                     binlog_pos: Optional[int] = None) -> bool:
        return self._apply(schema, table, "update", before, after, binlog_file, binlog_pos)

    def apply_delete(self, schema: str, table: str, row: Dict[str, Any],
                     binlog_file: Optional[str] = None,
                     binlog_pos: Optional[int] = None) -> bool:
        return self._apply(schema, table, "delete", row, None, binlog_file, binlog_pos)

    def _apply(
        self,
        schema: str,
        table: str,
        event_type: str,
        row: Dict[str, Any],
        after: Optional[Dict[str, Any]],
        binlog_file: Optional[str],
        binlog_pos: Optional[int],
    ) -> bool:
        self._begin_if_needed()

        meta = self._schema.get(schema, table)
        if meta is None:
            # 表在目标库/源库都不存在（可能是 DDL 尚未同步）。
            # 标记为等待 DDL，不永久拉黑——旧实现正是在这里把整表废掉。
            self._record_table_state(
                schema, table, "schema_missing", binlog_file, binlog_pos,
                "目标库缺少该表，等待 DDL 事件重建",
            )
            if self._record_failure(
                schema, table, event_type, ErrorClass.SCHEMA_MISSING, 1146,
                f"表 {schema}.{table} 不存在，事件已跳过",
                # 与 _handle_exec_error 保持同一负载结构，重放端点才能取到 before
                {"before": row}, binlog_file, binlog_pos,
            ):
                self.stats.failed += 1
            return False

        # binlog_row_metadata=MINIMAL 时行像只有占位键，按列顺序还原真实列名
        row_overflow = 0
        if ColumnMapper.needs_mapping(row):
            original = row
            row = ColumnMapper.remap(original, meta.columns)
            row_overflow = sum(1 for k in row if str(k).startswith("__overflow_"))
            if row_overflow:
                self._record_failure(
                    schema, table, event_type, ErrorClass.DATA, None,
                    f"行像包含 {row_overflow} 个超出表定义 {len(meta.columns)} 列的字段，"
                    "源库与目标库的表结构可能不一致",
                    {"before": _stringify(original)}, binlog_file, binlog_pos,
                )
                self.stats.failed += 1
                return False
            self._note_mapped(schema, table, list(row.keys()))

        if after is not None and ColumnMapper.needs_mapping(after):
            after = ColumnMapper.remap(after, meta.columns)

        try:
            generated = self._build(meta, event_type, row, after)
        except ValueError as exc:
            # 构造 SQL 失败属于数据处理问题：记 DLQ，跳过该行但继续推进位点
            if self._record_failure(
                schema, table, event_type, ErrorClass.DATA, None, str(exc),
                {"before": row}, binlog_file, binlog_pos,
            ):
                self.stats.failed += 1
            return False

        if generated.degraded and generated.reason:
            logger.warning(f"任务 {self.task_id} {schema}.{table}: {generated.reason}")

        try:
            cursor = self._connection().cursor()
            cursor.execute(generated.sql, generated.params)
            # rowcount 为 0 表示没有匹配到行（UPDATE/DELETE 空转），
            # 这可能意味着目标库与源库已经漂移，值得记录但不阻断。
            affected = cursor.rowcount
            cursor.close()
        except Exception as exc:
            return self._handle_exec_error(
                exc, schema, table, event_type, row, after, generated, binlog_file, binlog_pos
            )

        self._count(event_type)
        self._pending_rows += 1
        self._record_table_success(schema, table, binlog_file, binlog_pos)

        if affected == 0 and event_type in ("update", "delete"):
            logger.debug(
                f"任务 {self.task_id} {schema}.{table} {event_type} 影响 0 行，"
                "目标库可能已与源库漂移"
            )

        self._guard_transaction_size()
        return True

    def _build(self, meta: TableMeta, event_type: str, row: Dict[str, Any],
               after: Optional[Dict[str, Any]]):
        if event_type == "insert":
            return build_insert(meta, row)
        if event_type == "update":
            return build_update(meta, row, after or {})
        if event_type == "delete":
            return build_delete(meta, row)
        raise ValueError(f"未知事件类型: {event_type}")

    def _note_mapped(self, schema: str, table: str, columns: List[str]) -> None:
        """列名映射只在每张表首次发生时提示一次，避免刷屏。"""
        key = (schema, table)
        if key in self._mapped_notified:
            return
        self._mapped_notified.add(key)
        logger.info(
            f"任务 {self.task_id} {schema}.{table}: 源库 binlog_row_metadata=MINIMAL，"
            f"已按列顺序还原列名 {columns[:6]}{'...' if len(columns) > 6 else ''}"
        )

    def _count(self, event_type: str) -> None:
        if event_type == "insert":
            self.stats.inserted += 1
        elif event_type == "update":
            self.stats.updated += 1
        elif event_type == "delete":
            self.stats.deleted += 1

    # ------------------------------------------------------------ 错误处置

    def _handle_exec_error(
        self,
        exc: Exception,
        schema: str,
        table: str,
        event_type: str,
        row: Dict[str, Any],
        after: Optional[Dict[str, Any]],
        generated,
        binlog_file: Optional[str],
        binlog_pos: Optional[int],
    ) -> bool:
        cls, code = err_mod.classify_error(exc)
        message = str(exc)[:500]

        # 连接类：整体回滚并向上抛出，由引擎重连后从上次位点恢复
        if not err_mod.should_advance_checkpoint(cls):
            logger.error(
                f"任务 {self.task_id} 目标库连接异常，回滚当前事务: {message}"
            )
            self.rollback()
            raise TransientConnectionError(message) from exc

        if err_mod.to_dlq(cls):
            payload = {"before": row}
            if after is not None:
                payload["after"] = after
            if self._record_failure(
                schema, table, event_type, cls, code, message,
                payload, binlog_file, binlog_pos,
                sql=generated.sql if generated else None,
            ):
                self.stats.failed += 1

        if err_mod.mark_table_broken(cls):
            self._record_table_state(
                schema, table, "schema_missing", binlog_file, binlog_pos, message
            )
            # 结构变化后缓存已失效
            self._schema.invalidate(schema, table)

        return False

    def _record_failure(
        self,
        schema: str,
        table: str,
        event_type: str,
        error_class: ErrorClass,
        error_code: Optional[int],
        message: str,
        payload: Dict[str, Any],
        binlog_file: Optional[str],
        binlog_pos: Optional[int],
        sql: Optional[str] = None,
    ) -> bool:
        """
        记录一条失败事件。

        同一事务内同表同类错误只记一次（避免异常风暴刷爆 DLQ），
        返回是否真的记录了一条新事件，用于精确计数。
        """
        key = (schema, table, event_type, error_class)
        if key in self._reported:
            return False
        self._reported.add(key)

        event = FailedEvent(
            schema=schema,
            table=table,
            event_type=event_type,
            error_class=error_class,
            error_code=error_code,
            error_message=message,
            payload={**payload, **({"sql": sql} if sql else {})},
            binlog_file=binlog_file,
            binlog_pos=binlog_pos,
        )
        self.pending_errors.append(event)
        if self._dlq is not None:
            try:
                self._dlq(event)
            except Exception as exc:  # DLQ 写入失败不能影响同步主流程
                logger.error(f"记录失败事件到 DLQ 时出错: {exc}")

        level = "WARNING" if error_class in (ErrorClass.DATA, ErrorClass.DUPLICATE) else "ERROR"
        logger.log(level, f"任务 {self.task_id} {schema}.{table} {event_type} 未应用: {message}")
        return True

    def _guard_transaction_size(self) -> None:
        """
        超大事务保护。

        超过阈值时先提交一次，牺牲单事务原子性换取内存与锁安全，
        并在日志中明确告警。
        """
        if self._pending_rows >= self._tx_max_rows:
            logger.warning(
                f"任务 {self.task_id} 单事务超过 {self._tx_max_rows} 行，"
                "提前提交（该事务不再具备原子性）"
            )
            try:
                self._conn.commit()
            finally:
                self._in_tx = False
                self._pending_rows = 0
                self._reported.clear()

    # ------------------------------------------------------------ DDL

    def apply_ddl(self, query: str, schema: Optional[str], table: Optional[str] = None) -> bool:
        """
        执行 DDL。

        DDL 会隐式提交当前事务，因此先提交已累积的变更，再单独执行并提交。
        """
        if self._in_tx:
            try:
                self._conn.commit()
            finally:
                self._in_tx = False
                self._pending_rows = 0
                self._reported.clear()

        try:
            cursor = self._connection().cursor()
            cursor.execute(query)
            try:
                # 结果集需要消费掉，否则连接状态错乱
                cursor.fetchall()
            except Exception:
                pass
            cursor.close()
            self._conn.commit()
        except Exception as exc:
            cls, code = err_mod.classify_error(exc)
            if not err_mod.should_advance_checkpoint(cls):
                self.rollback()
                raise TransientConnectionError(str(exc)) from exc

            # DDL 失败会让后续所有行事件都写不进去，因此必须留下痕迹：
            # 既要进 DLQ，也要在日志里以 ERROR 级别出现，
            # 否则表结构不一致会表现为大量 1054 而找不到第一现场。
            self._record_failure(
                schema or "", table or "", "ddl", cls, code,
                str(exc)[:500], {"query": query}, None, None, sql=query,
            )
            self.stats.failed += 1
            logger.error(
                f"任务 {self.task_id} DDL 执行失败（后续行事件可能受影响）: "
                f"{str(exc)[:200]} | SQL: {query[:200]}"
            )
            return False

        # 结构变了，清缓存。表名未知时按库清空。
        if table:
            self._schema.invalidate(schema or "", table)
        elif schema:
            self._schema.invalidate(schema)

        self.stats.ddl += 1
        logger.info(f"任务 {self.task_id} DDL 已应用: {query[:120]}")
        return True

    # ------------------------------------------------------------ 回调

    def _record_table_state(
        self, schema: str, table: str, state: str,
        binlog_file: Optional[str], binlog_pos: Optional[int], message: Optional[str],
    ) -> None:
        if self._table_state is None:
            return
        try:
            self._table_state(schema, table, state, binlog_file, binlog_pos, 0, message)
        except Exception as exc:
            logger.error(f"更新表状态失败: {exc}")

    def _record_table_success(
        self, schema: str, table: str,
        binlog_file: Optional[str], binlog_pos: Optional[int],
    ) -> None:
        if self._table_state is None:
            return
        try:
            self._table_state(schema, table, "active", binlog_file, binlog_pos, 1, None)
        except Exception as exc:
            logger.error(f"更新表状态失败: {exc}")
