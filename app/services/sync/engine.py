"""
binlog 消费引擎。

一个任务一个线程。相比旧实现的变化：

- 非 daemon 线程 + 显式停机信号，保证退出前位点落盘。
- 逐事件累积、XID 边界提交，源库事务在目标库原子落地。
- 错误分级处置，连接类错误不推进位点并退避重连。
- 表状态与 DLQ 落库，健康度可查。
- 启动前校验 binlog_format=ROW 与 binlog_row_image=FULL。
"""
import json
import random
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import mysql.connector
import pymysql
from loguru import logger
from sqlalchemy.exc import IntegrityError

from ...core.config import now_beijing, settings
from ...core.database import session_scope
from ...models.database import SyncError, SyncStatus, SyncTableState, TableSyncState
from ..mysql.conninfo import build_connect_kwargs, connect_plain_fallback
from ..mysql.schema import SchemaResolver
from . import errors as err_mod
from .applier import Applier, FailedEvent, TransientConnectionError
from .checkpoint import CheckpointStore, load_checkpoint

# pymysqlreplication 事件
try:  # pragma: no cover - 导入失败时给出明确提示
    from pymysqlreplication import BinLogStreamReader
    from pymysqlreplication.event import QueryEvent, RotateEvent, XidEvent
    from pymysqlreplication.row_event import (
        DeleteRowsEvent,
        UpdateRowsEvent,
        WriteRowsEvent,
    )
except ImportError as exc:  # pragma: no cover
    raise ImportError("缺少 mysql-replication 依赖，无法启动同步引擎") from exc

_DDL_PREFIXES = ("CREATE", "ALTER", "DROP", "RENAME", "TRUNCATE")

# MySQL 字段类型码，用于识别需要补齐定义的 ENUM/SET 列。
# 取自 pymysql.constants.FIELD_TYPE，写死数值以免依赖其内部结构。
_ENUM_TYPE = 247   # FIELD_TYPE.ENUM
_SET_TYPE = 248    # FIELD_TYPE.SET

# mysql-replication 1.0.5/1.0.6 的 fetchone() 里写了 logging.WARN(...)，
# 这在 Python 3 是 int 常量（30）而非函数。该分支本意是处理「连接被源库掐断」，
# 属于库自身会自动重连的**正常路径**，但一旦走到这里就抛
# TypeError: 'int' object is not callable，把同步线程直接打死。
# 1.0.7 起修复。此处做启动期硬校验：宁可在启动时就报错，也不要半夜里静默掉线。
_MIN_READER_LIB_VERSION = (1, 0, 7)


def _reader_lib_version() -> Optional[tuple]:
    """取 mysql-replication 的已安装版本，取不到返回 None。"""
    try:
        from importlib.metadata import version as _pkg_version

        raw = _pkg_version("mysql-replication")
    except Exception:
        return None
    parts: List[int] = []
    for chunk in str(raw).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


def assert_reader_lib_sane() -> None:
    """
    启动期校验 binlog 读取库版本。

    版本过低时库会在「连接被掐断」这一正常路径上崩溃，且崩溃点在线程内部，
    外部只能看到任务莫名失败。提前拒绝启动能让问题在部署阶段就暴露。
    """
    installed = _reader_lib_version()
    if installed is None:
        # 元数据缺失（如源码直跑）：退化为运行时探测，直接看源码里有没有这个 bug。
        try:
            import inspect

            src = inspect.getsource(BinLogStreamReader.fetchone)
        except Exception:
            return
        if "logging.WARN(" in src:
            raise SyncConfigError(
                "mysql-replication 版本过低（存在 logging.WARN 调用缺陷），"
                "会在源库断开空闲连接时崩溃。请升级：pip install -U mysql-replication"
            )
        return

    if installed < _MIN_READER_LIB_VERSION:
        want = ".".join(str(x) for x in _MIN_READER_LIB_VERSION)
        got = ".".join(str(x) for x in installed)
        raise SyncConfigError(
            f"mysql-replication 版本过低（当前 {got}，需要 >= {want}）。"
            "低版本在源库掐断空闲连接时调用 logging.WARN()——该符号在 Python 3 "
            "是整数 30，会抛 TypeError 并终止同步线程。"
            "修复：pip install -U 'mysql-replication>=1.0.7'"
        )


def _norm_schema(value: Any) -> str:
    """
    规范化 schema 名。

    pymysqlreplication 的 QueryEvent.schema 是 **bytes**（如 b'dbsync_src'），
    而 RowsEvent.schema 是 str。两者直接比较永远不相等——
    这会让所有源库 DDL 被误判为「非源库事件」而静默跳过。
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _mask(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """日志用：去掉密码。"""
    return {k: ("***" if k == "password" else v) for k, v in cfg.items()}


class SyncAborted(Exception):
    """任务被主动停止。"""


class SyncConfigError(Exception):
    """源库 binlog 配置不满足同步要求。"""


class BinlogReader:
    """
    binlog 事件读取器。

    抽出这一层是为了让引擎主循环可被测试：真实环境需要 MySQL，
    单测中替换为回放固定事件序列的假实现。
    """

    def __init__(self, source_config: Dict[str, Any]) -> None:
        self._cfg = source_config

    def read_server_capabilities(self) -> Dict[str, Optional[str]]:
        """读取源库 binlog 相关配置。"""
        result: Dict[str, Optional[str]] = {
            "format": None, "row_image": None, "row_metadata": None, "gtid_mode": None,
        }
        conn = connect_plain_fallback(
            **build_connect_kwargs(
                host=self._cfg["host"],
                port=self._cfg["port"],
                user=self._cfg["user"],
                password=self._cfg["password"],
            )
        )
        try:
            cursor = conn.cursor()
            for key, column in (
                ("binlog_format", "format"),
                ("binlog_row_image", "row_image"),
                ("binlog_row_metadata", "row_metadata"),
                ("gtid_mode", "gtid_mode"),
            ):
                try:
                    cursor.execute(f"SHOW VARIABLES LIKE '{key}'")
                    row = cursor.fetchone()
                    if row:
                        result[column] = str(row[1]).upper()
                except Exception:
                    pass
            cursor.close()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return result

    def current_position(self) -> Dict[str, Optional[str]]:
        """
        读取当前 binlog 位点。

        兼容 MySQL 8.4+（SHOW BINARY LOG STATUS）与 5.7/8.0（SHOW MASTER STATUS）。

        两种命令的列布局不同：8.4 返回 (File, Position, Binlog_Do_DB,
        Binlog_Ignore_DB, Executed_Gtid_Set)，5.7 返回 (File, Position,
        Binlog_Do_DB, Binlog_Ignore_DB)。因此 GTID 只能在 gtid_mode 开启
        且列数足够时读取，否则留空——不能盲目取第 5 列。
        """
        conn = connect_plain_fallback(
            **build_connect_kwargs(
                host=self._cfg["host"],
                port=self._cfg["port"],
                user=self._cfg["user"],
                password=self._cfg["password"],
            )
        )
        try:
            cursor = conn.cursor()
            row = None
            for sql in ("SHOW BINARY LOG STATUS", "SHOW MASTER STATUS"):
                try:
                    cursor.execute(sql)
                    row = cursor.fetchone()
                    if row:
                        break
                except Exception:
                    continue

            gtid = None
            if row and len(row) > 4 and row[4]:
                gtid = str(row[4]).strip() or None

            cursor.close()
            if not row:
                raise SyncConfigError(
                    "无法获取 binlog 位点，请确认源库已启用 binlog "
                    "且当前账号具备 REPLICATION CLIENT 权限"
                )
            return {"file": row[0], "pos": int(row[1]), "gtid": gtid}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stream(self, *, log_file: Optional[str], log_pos: Optional[int], server_id: int,
               blocking: bool = True):
        """
        创建 binlog 流。

        only_schemas 必须限定为源库：否则会消费整台 MySQL 上所有库的事件，
        既浪费资源，也会把无关库的变更写到目标库去。
        """
        pymysql_settings = {
            "host": self._cfg["host"],
            "port": self._cfg["port"],
            "user": self._cfg["user"],
            "password": self._cfg["password"],
        }
        return BinLogStreamReader(
            connection_settings=pymysql_settings,
            server_id=server_id,
            log_file=log_file,
            log_pos=log_pos,
            only_events=[WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent,
                         QueryEvent, RotateEvent, XidEvent],
            only_schemas=[self._cfg["database"]],
            blocking=blocking,
            resume_stream=True,
            # 心跳：源库空闲超过 wait_timeout 会单方面掐断连接（表现为
            # OperationalError 2006/2013）。库虽有自动重连逻辑，但依赖
            # 「断线后再报错」本身就要多绕一圈重连；主动心跳让连接保持活跃，
            # 从源头上避免这个循环。取值需远小于源库 wait_timeout。
            slave_heartbeat=settings.SYNC_HEARTBEAT_SECONDS,
        )


class SyncEngine:
    """单个同步任务的运行时。"""

    def __init__(
        self,
        *,
        task_id: int,
        task_name: str,
        source_config: Dict[str, Any],
        target_config: Dict[str, Any],
        initial: Any = None,
        server_id: Optional[int] = None,
        reader: Optional[BinlogReader] = None,
        schema_resolver_factory: Optional[Callable[[Callable[[], Any]], Any]] = None,
    ) -> None:
        self.task_id = task_id
        self.task_name = task_name
        self.source_config = source_config
        self.target_config = target_config
        # 源库名（binlog 事件携带的 schema）与目标库名（写入落点）。
        # 两者可能不同：同实例跨库同步时正是如此。
        self.source_schema = source_config.get("database") or ""
        self.target_schema = target_config.get("database") or ""

        # 目标库名缺失时必须立刻失败，绝不能猜。
        # 若退化为源库名，写入 SQL 会构造成 `源库`.`表`——原地写回源库，
        # 既污染数据又造成 binlog 无限循环，而且不会有任何错误提示。
        if not self.target_schema:
            raise SyncConfigError(
                "无法确定目标库名：目标数据库配置缺少 database_name。"
                "请检查「数据库管理」中该记录的库名是否为空。"
            )
        self.initial = initial
        self.server_id = server_id or random.randint(
            settings.SYNC_SERVER_ID_BASE,
            settings.SYNC_SERVER_ID_BASE + settings.SYNC_SERVER_ID_RANGE,
        )

        self._reader = reader or BinlogReader(source_config)
        # 表结构解析器的构造可替换，便于在无真实 MySQL 时测试消费循环
        self._resolver_factory = schema_resolver_factory or SchemaResolver
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()

        self._checkpoint: Optional[CheckpointStore] = None
        self._applier: Optional[Applier] = None
        self._owns_applier = True
        self._schema: Optional[Any] = None
        self._target_conn = None
        self._stream = None
        self._retry_delay = settings.SYNC_RETRY_BASE_DELAY
        self.last_error: Optional[str] = None
        # 退出原因：stopped（正常停止）/ failed（异常崩溃）/ None（仍在运行）。
        # 主管进程靠它区分「用户按了停止」和「线程自己死了」。
        self._exit_reason: Optional[str] = None

    @property
    def exit_reason(self) -> Optional[str]:
        return self._exit_reason

    @property
    def has_failed(self) -> bool:
        """线程是否因异常退出（而非被正常停止）。"""
        return self._exit_reason == "failed"

    # ------------------------------------------------------------ 生命周期

    @property
    def is_running(self) -> bool:
        return self._running.is_set() and not self._stop_event.is_set()

    def start(self) -> None:
        """
        启动消费线程。

        用 daemon 线程：停机时会先尝试优雅停止（位点落盘），
        若因网络阻塞未能及时退出，也不应拖着整个进程不结束。
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning(f"同步任务 {self.task_id} 已在运行")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_guarded, name=f"sync-{self.task_id}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """
        请求停止并等待线程收尾（含位点落盘）。

        关键：仅设置停止标志是不够的——消费线程通常阻塞在网络读取上
        （等待 binlog 事件），标志要等到下一个事件到达才会被检查。
        因此这里主动关闭 binlog 流连接，让阻塞读取立即抛错返回，
        线程才能走到停机逻辑完成位点落盘。
        """
        self._stop_event.set()

        # 打断阻塞中的流读取
        self._interrupt_stream()

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                # 兜底：再关一次并在日志中明确告警。
                # 线程是 daemon，不会阻止进程退出。
                logger.error(
                    f"同步任务 {self.task_id} 未能在 {timeout}s 内停止，"
                    "位点可能未落盘（下次启动会从上次保存的位置重放）"
                )
                self._interrupt_stream()
        self._running.clear()

    def _interrupt_stream(self) -> None:
        """
        关闭底层流连接，打断阻塞读取。

        pymysqlreplication 在 _read_packet() 上无超时地等待数据，
        socket 关闭会让它立刻抛出异常，从而脱离阻塞。
        异常会被消费循环捕获并进入正常的停机路径。
        """
        stream = self._stream
        if stream is None:
            return
        try:
            # 先关底层 socket：close() 本身在阻塞读取中可能等待
            conn = getattr(stream, "_stream_connection", None)
            if conn is not None:
                sock = getattr(conn, "_sock", None) or getattr(conn, "socket", None)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    try:
                        sock.close()
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ------------------------------------------------------------ 主循环

    def _run_guarded(self) -> None:
        self._running.set()
        try:
            self._run()
            self._exit_reason = "stopped"
        except SyncAborted:
            logger.info(f"同步任务 {self.task_id} 已停止")
            self._exit_reason = "stopped"
        except Exception as exc:
            logger.exception(f"同步任务 {self.task_id} 异常退出: {exc}")
            self.last_error = str(exc)
            # 记录崩溃事实：主管进程据此判定「线程死了但任务没被停止」，
            # 从而触发自愈。旧实现只把状态写进库，人不去翻日志就永远发现不了。
            self._exit_reason = "failed"
            if self._checkpoint is not None:
                self._checkpoint.set_status(SyncStatus.FAILED, str(exc)[:1000])
                self._checkpoint.set_health("stalled")
                self._checkpoint.close()
        finally:
            self._teardown()
            self._running.clear()

    def _run(self) -> None:
        checkpoint = CheckpointStore(
            self.task_id, interval=settings.SYNC_CHECKPOINT_INTERVAL
        )
        self._checkpoint = checkpoint

        try:
            self._verify_source_binlog(checkpoint)

            while not self._stop_event.is_set():
                try:
                    self._consume_once(checkpoint)
                    # 正常返回说明流被关闭（blocking=False 或连接断开）
                    if self._stop_event.is_set():
                        break
                    raise TransientConnectionError("binlog 流已断开")
                except SyncConfigError:
                    raise
                except TransientConnectionError as exc:
                    # 连接类：退避重连，位点不推进
                    logger.warning(
                        f"同步任务 {self.task_id} 连接中断，{self._retry_delay:.0f}s 后重试: {exc}"
                    )
                    checkpoint.set_health("degraded")
                    checkpoint.flush()
                    if self._sleep_interruptible(self._retry_delay):
                        break
                    self._retry_delay = min(
                        self._retry_delay * 2, settings.SYNC_RETRY_MAX_DELAY
                    )
                else:
                    self._retry_delay = settings.SYNC_RETRY_BASE_DELAY
        finally:
            # 停机必须落盘：这是旧实现最大的漏洞
            checkpoint.set_status(SyncStatus.STOPPED)
            checkpoint.set_health("unknown")
            checkpoint.close()

    def _consume_once(self, checkpoint: CheckpointStore) -> None:
        """建立连接并消费 binlog，直到流断开或被停止。"""
        self._connect_target()
        self._schema = self._resolver_factory(self._get_target_connection, self.target_schema)

        # 已由外部注入的 Applier 不重建（测试与重放场景）
        if self._applier is None:
            self._applier = self._build_applier()
            self._owns_applier = True
        else:
            self._owns_applier = False

        position = checkpoint.position
        if not position.is_valid():
            # 首次启动：向源库查询当前位点。
            #
            # 注意不能写成 `self.initial or self._reader.current_position()`——
            # manager 传进来的 initial 恒为字典（键存在但值为 None），
            # 空字典判断永远为真，会导致位点保持 None。
            # 位点为 None 时 pymysqlreplication 会内部执行 SHOW MASTER STATUS，
            # 该命令在 MySQL 8.4+ 已被移除，直接抛 1064。
            file_from_initial = (self.initial or {}).get("file")
            pos_from_initial = (self.initial or {}).get("pos")
            gtid_from_initial = (self.initial or {}).get("gtid")

            if file_from_initial and pos_from_initial is not None:
                checkpoint.update_position(
                    binlog_file=file_from_initial,
                    binlog_pos=pos_from_initial,
                    gtid_set=gtid_from_initial or None,
                )
            else:
                # 无有效位点可恢复，从源库当前最新位点开始
                initial = self._reader.current_position()
                checkpoint.update_position(
                    binlog_file=initial.get("file"),
                    binlog_pos=initial.get("pos"),
                    gtid_set=initial.get("gtid") or None,
                )

            checkpoint.flush()
            position = checkpoint.position

        # 兜底：位点必须完整，否则会落到库内部的 SHOW MASTER STATUS（8.4 不支持）
        if not (position.binlog_file and position.binlog_pos is not None):
            raise SyncConfigError(
                "无法确定 binlog 起始位点：源库未返回有效的文件与偏移量，"
                "请确认账号具备 REPLICATION CLIENT 权限且已启用 binlog"
            )

        logger.info(
            f"同步任务 {self.task_id} 开始消费: "
            f"{position.binlog_file}@{position.binlog_pos} (server_id={self.server_id})"
        )
        checkpoint.set_status(SyncStatus.RUNNING)
        checkpoint.set_health("healthy")

        stream = self._reader.stream(
            log_file=position.binlog_file,
            log_pos=position.binlog_pos,
            server_id=self.server_id,
            blocking=True,
        )
        self._stream = stream

        try:
            self._drain(stream, checkpoint)
        finally:
            try:
                stream.close()
            except Exception:
                pass
            self._stream = None
            if self._applier is not None:
                # 流中断：未提交的事务必须回滚，位点停在上一个已提交点
                self._applier.rollback()
                if self._owns_applier:
                    self._applier.close()
                    self._applier = None
            self._schema = None

    def _build_applier(self) -> Applier:
        return Applier(
            task_id=self.task_id,
            connection_provider=self._get_target_connection,
            schema_resolver=self._schema,
            dlq_recorder=self._record_dlq,
            table_state_recorder=self._record_table_state,
            tx_max_rows=settings.SYNC_TX_MAX_ROWS,
        )

    def _drain(self, stream, checkpoint: CheckpointStore) -> None:
        """
        事件消费循环。

        与连接建立分离，便于在无真实 MySQL 的环境下验证事件分发逻辑。

        停机时底层流连接会被主动关闭（见 _interrupt_stream），
        阻塞中的读取随即抛错。那种情况属于预期停机，不作为异常上报。
        """
        try:
            iterator = iter(stream)
            while True:
                if self._stop_event.is_set():
                    raise SyncAborted()

                try:
                    event = next(iterator)
                except StopIteration:
                    return
                except SyncAborted:
                    raise
                except Exception:
                    # 流已被 stop() 关闭 → 正常停机路径
                    if self._stop_event.is_set():
                        raise SyncAborted() from None
                    raise

                if self._stop_event.is_set():
                    raise SyncAborted()

                self._dispatch(event, checkpoint)
        except SyncAborted:
            raise

    def _dispatch(self, event, checkpoint: CheckpointStore) -> None:
        """按事件类型分派。"""
        if isinstance(event, RotateEvent):
            # 每次建立 binlog 流时，源库都会先发一个 Rotate 事件告知当前文件。
            # 若该事件指向的正是我们正在消费的文件，偏移量必须保持不变——
            # 否则位点会被重置到 4，整份 binlog 从头重放。
            # 只有真正轮转到新文件时才从头部开始。
            current = checkpoint.position
            if event.next_binlog != current.binlog_file:
                checkpoint.update_position(
                    binlog_file=event.next_binlog, binlog_pos=4
                )
            else:
                checkpoint.update_position(binlog_file=event.next_binlog)
            return

        if isinstance(event, XidEvent):
            # 事务提交边界：目标库在此刻原子落地
            stats = self._applier.commit()
            if stats.total or stats.failed:
                checkpoint.record_events(stats.total)
                checkpoint.record_dlq(stats.failed)
                checkpoint.update_position(
                    binlog_file=checkpoint.position.binlog_file,
                    binlog_pos=self._event_pos(event),
                )
            checkpoint.maybe_flush()
            self._maybe_mark_stalled(checkpoint)
            return

        if isinstance(event, QueryEvent):
            self._handle_query_event(event, checkpoint)
            return

        self._handle_row_event(event, checkpoint)

    # ------------------------------------------------------------ 事件处理

    def _event_pos(self, event) -> Optional[int]:
        try:
            return int(event.packet.log_pos)
        except Exception:
            return None

    def _rewrite_schema(self, query: str) -> str:
        """
        把 DDL 中的源库名改写为目标库名。

        binlog 的 DDL 语句形如 ``ALTER TABLE `src`.`t` ...`` 或
        ``CREATE DATABASE `src```。同实例跨库同步时，直接转发会作用在源库上，
        因此必须替换。仅替换被反引号包裹的精确匹配，避免误伤表名或字符串字面量。
        """
        if not self.source_schema or self.source_schema == self.target_schema:
            return query
        return query.replace(
            f"`{self.source_schema}`", f"`{self.target_schema}`"
        )

    def _handle_query_event(self, event, checkpoint: CheckpointStore) -> None:
        """
        处理 QueryEvent。

        BEGIN 是事务开始标记，跳过；DDL 需要转发。
        
        重要：若这里看到非 DDL 的业务语句，说明源库 binlog_format 不是 ROW，
        而这会导致 DML 丢失。必须显式告警，不能像旧实现那样静默 continue。
        """
        query = (event.query or "").strip()
        if not query:
            return

        upper = query.upper()
        if upper == "BEGIN":
            return

        if upper.startswith(_DDL_PREFIXES):
            # QueryEvent.schema 是 bytes，必须规范化后再比较
            schema = _norm_schema(getattr(event, "schema", None))
            # 只同步源库的 DDL；schema 为空时（部分语句不带库名）按源库处理
            if schema and schema != self.source_schema:
                return
            if self._applier is not None:
                self._applier.apply_ddl(self._rewrite_schema(query), self.target_schema)
            checkpoint.flush()
            return

        # 非 DDL 的 Query 事件 = statement 格式的 DML
        self._warn_statement_dml(query, checkpoint)

    def _ensure_enum_labels(self, event, schema: str, table: str) -> None:
        """
        为缺定义的 ENUM/SET 列补齐候选值。

        背景（真实缺陷）：源库 binlog_row_metadata=MINIMAL 时，MySQL 不下发
        ENUM_STR_VALUE 元数据，pymysqlreplication 于是把这些列解码成 **None**：

            elif column.type == FIELD_TYPE.ENUM:
                if column.enum_values:                 # MINIMAL 下恒为假
                    return column.enum_values[...]
                self.packet.read_uint_by_size(...)     # 读掉字节
                return None                            # ← 真实值被丢弃

        后果是 NOT NULL 的 ENUM 列写入报 1048（Column cannot be null）、
        可空 ENUM 列被静默写成 NULL。这与「UPDATE 误写未修改列」是同类数据
        损坏，但触发条件更隐蔽：只在 MINIMAL 元数据下出现。

        修法：从目标库的 information_schema 读出真实候选值，
        按列序号回填到事件列定义上，让库按正常路径解码。
        读取顺序即 ORDINAL_POSITION，与 binlog 中的列顺序一致。
        """
        columns = getattr(event, "columns", None)
        if not columns:
            return

        # 快路径：全部列都无需补（表里没有 ENUM/SET，或库已自带定义）
        if all(getattr(c, "enum_values", None) or getattr(c, "set_values", None)
               or getattr(c, "type", None) not in (_ENUM_TYPE, _SET_TYPE)
               for c in columns):
            return

        meta = self._schema.get(schema, table) if self._schema is not None else None
        if meta is None:
            return

        for index, column in enumerate(columns):
            ctype = getattr(column, "type", None)
            if ctype == _ENUM_TYPE and not getattr(column, "enum_values", None):
                labels = meta.enum_labels_at(index)
                if labels:
                    # 库里约定 enum_values[0] 为空串占位（下标 1 起才是真实值）
                    column.enum_values = [""] + list(labels)
            elif ctype == _SET_TYPE and not getattr(column, "set_values", None):
                labels = meta.set_labels_at(index)
                if labels:
                    column.set_values = list(labels)

    def _warn_statement_dml(self, query: str, checkpoint: CheckpointStore) -> None:
        message = (
            f"检测到 statement 格式的 SQL 事件，源库 binlog_format 可能不是 ROW，"
            f"该语句的变更无法通过行事件复制: {query[:200]}"
        )
        logger.error(f"任务 {self.task_id} {message}")
        checkpoint.set_health("degraded")
        self._record_dlq(
            FailedEvent(
                schema=getattr(self, "_current_schema", "") or "",
                table=getattr(self, "_current_table", "") or "",
                event_type="query",
                error_class=err_mod.ErrorClass.FATAL,
                error_code=None,
                error_message=message,
                payload={"query": query[:2000]},
            )
        )
        checkpoint.record_dlq()
        checkpoint.flush()

    def _handle_row_event(self, event, checkpoint: CheckpointStore) -> None:
        """处理 WriteRowsEvent / UpdateRowsEvent / DeleteRowsEvent。"""
        table = event.table
        # 写入落点是目标库，而非事件里的源库名
        schema = self.target_schema
        self._current_schema = schema
        self._current_table = table

        # 必须在读取 event.rows 之前补齐 ENUM/SET 定义：
        # rows 是惰性解码的（首次访问时才逐列解析）。一旦开始解码，
        # 缺定义的 ENUM 列已经被读成 None，再补也来不及。
        self._ensure_enum_labels(event, schema, table)

        pos = self._event_pos(event)
        ts = getattr(event, "timestamp", None)

        applied_any = False

        if isinstance(event, WriteRowsEvent):
            for row in event.rows:
                values = row.get("values", row)
                if self._applier.apply_insert(schema, table, values, checkpoint.position.binlog_file, pos):
                    applied_any = True

        elif isinstance(event, UpdateRowsEvent):
            for row in event.rows:
                before = row.get("before_values", {})
                after = row.get("after_values", {})
                if self._applier.apply_update(
                    schema, table, before, after, checkpoint.position.binlog_file, pos
                ):
                    applied_any = True

        elif isinstance(event, DeleteRowsEvent):
            for row in event.rows:
                values = row.get("values", row)
                if self._applier.apply_delete(schema, table, values, checkpoint.position.binlog_file, pos):
                    applied_any = True

        if applied_any and ts:
            checkpoint.record_events(0, ts)
            checkpoint.maybe_flush()

    # ------------------------------------------------------------ 健康度

    def _maybe_mark_stalled(self, checkpoint: CheckpointStore) -> None:
        """
        长时间无位点推进 → 标记 stalled。

        旧实现永远显示 running，即使事件早已被静默丢弃。
        """
        stalled_after = max(300.0, settings.SYNC_RETRY_MAX_DELAY * 5)
        if checkpoint.seconds_since_progress > stalled_after:
            checkpoint.set_health("stalled")
        elif checkpoint.metrics.dlq_count:
            checkpoint.set_health("degraded")
        else:
            checkpoint.set_health("healthy")

    # ------------------------------------------------------------ 持久化回调

    def _record_dlq(self, event: FailedEvent) -> None:
        """写入失败事件队列。"""
        try:
            with session_scope() as db:
                # DLQ 上限保护：超出时丢弃最早记录
                total = db.query(SyncError).filter(SyncError.task_id == self.task_id).count()
                if total >= settings.DLQ_MAX_ROWS:
                    oldest = (
                        db.query(SyncError)
                        .filter(SyncError.task_id == self.task_id)
                        .order_by(SyncError.id.asc())
                        .limit(max(1, settings.DLQ_MAX_ROWS // 10))
                        .all()
                    )
                    for row in oldest:
                        db.delete(row)

                db.add(
                    SyncError(
                        task_id=self.task_id,
                        schema_name=event.schema or "",
                        table_name=event.table or "",
                        event_type=event.event_type,
                        error_code=event.error_code,
                        error_message=event.error_message[:2000],
                        payload=json.dumps(event.payload, default=str, ensure_ascii=False)[:20000],
                        binlog_file=event.binlog_file,
                        binlog_pos=event.binlog_pos,
                    )
                )
        except Exception as exc:
            logger.error(f"写入 DLQ 失败: {exc}")

    def _record_table_state(
        self,
        schema: str,
        table: str,
        state: str,
        binlog_file: Optional[str],
        binlog_pos: Optional[int],
        delta: int,
        message: Optional[str],
    ) -> None:
        """更新单表同步状态。"""
        try:
            with session_scope() as db:
                row = (
                    db.query(SyncTableState)
                    .filter(
                        SyncTableState.task_id == self.task_id,
                        SyncTableState.schema_name == schema,
                        SyncTableState.table_name == table,
                    )
                    .first()
                )
                if row is None:
                    row = SyncTableState(
                        task_id=self.task_id, schema_name=schema, table_name=table
                    )
                    db.add(row)
                    try:
                        db.flush()
                    except IntegrityError:
                        # 并发场景下另一处已插入同一行（任务的表状态可能被
                        # 消费线程与重放接口同时更新）。回滚本次插入后重新查询。
                        db.rollback()
                        row = (
                            db.query(SyncTableState)
                            .filter(
                                SyncTableState.task_id == self.task_id,
                                SyncTableState.schema_name == schema,
                                SyncTableState.table_name == table,
                            )
                            .first()
                        )
                        if row is None:
                            raise

                try:
                    new_state = TableSyncState(state)
                except ValueError:
                    new_state = TableSyncState.ACTIVE

                # 记录定位键信息：无主键/唯一键的表在界面上要能看出风险。
                # 此前 pk_columns 从未被填充，用户无法从界面得知
                # 某张表的 UPDATE/DELETE 是靠全列匹配在硬撑。
                self._refresh_locator_info(row, schema, table)

                if binlog_file:
                    row.last_binlog_file = binlog_file
                if binlog_pos is not None:
                    row.last_binlog_pos = binlog_pos
                if delta:
                    row.applied_events = (row.applied_events or 0) + delta
                    row.last_applied_at = now_beijing()

                if message:
                    row.last_error = message[:2000]
                    row.error_count = (row.error_count or 0) + 1
                    # 仅在确属结构缺失时才标为 schema_missing；
                    # 其它告警不应覆盖已有状态。
                    if new_state is TableSyncState.SCHEMA_MISSING:
                        row.state = TableSyncState.SCHEMA_MISSING
                else:
                    # 无消息即正常应用，清掉上一轮的错误信息
                    row.state = new_state
                    row.last_error = None

                # 无可靠定位键的表标记出来，便于界面提示风险
                if row.state is TableSyncState.ACTIVE and not (
                    row.pk_columns or row.unique_keys
                ):
                    row.state = TableSyncState.NO_PRIMARY_KEY
        except Exception as exc:
            logger.error(f"更新表状态失败: {exc}")

    def _refresh_locator_info(self, row: SyncTableState, schema: str, table: str) -> None:
        """
        从表结构解析结果回填主键与唯一键信息。

        这些信息用于在界面上区分「定位可靠」与「靠全列匹配硬撑」两类表——
        后者在有重复行时 DELETE 会误删多行。
        """
        if self._schema is None:
            return
        meta = self._schema.get(schema, table)
        if meta is None:
            return
        row.pk_columns = list(meta.pk_columns) if meta.pk_columns else None
        row.unique_keys = [list(k) for k in meta.unique_keys] if meta.unique_keys else None

    # ------------------------------------------------------------ 连接

    def _connect_target(self):
        try:
            self._target_conn = connect_plain_fallback(
                **build_connect_kwargs(
                    host=self.target_config["host"],
                    port=self.target_config["port"],
                    user=self.target_config["user"],
                    password=self.target_config["password"],
                    # 必须指定默认库：源库的 DDL 往往不带库名限定符
                    # （如 ALTER TABLE users ADD COLUMN x），没有默认库会直接报
                    # "No database selected"，导致表结构变更无法同步。
                    database=self.target_schema or None,
                    # 同步写入依赖显式事务边界（XID 提交），不能自动提交
                    autocommit=False,
                )
            )
            logger.info(
                f"任务 {self.task_id} 已连接目标库: "
                f"{self.target_config['host']}:{self.target_config['port']}"
                f"/{self.target_schema}"
            )
        except Exception as exc:
            raise TransientConnectionError(f"目标库连接失败: {exc}") from exc
        return self._target_conn

    def _get_target_connection(self):
        """供 Applier 与 SchemaResolver 使用。断线时重连。"""
        conn = self._target_conn
        if conn is None:
            return self._connect_target()
        try:
            if not conn.is_connected():
                raise TransientConnectionError("目标库连接已断开")
        except TransientConnectionError:
            raise
        except Exception:
            # 部分驱动在坏连接上 is_connected 会抛错，直接重连
            return self._connect_target()
        return conn

    def close_target(self) -> None:
        if self._target_conn is not None:
            try:
                self._target_conn.close()
            except Exception:
                pass
            self._target_conn = None

    def _teardown(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._applier is not None:
            try:
                self._applier.close()
            except Exception:
                pass
            self._applier = None
        self.close_target()

    # ------------------------------------------------------------ 辅助

    def _sleep_interruptible(self, seconds: float) -> bool:
        """可被打断的休眠。返回 True 表示收到停止信号。"""
        return self._stop_event.wait(timeout=seconds)

    def _verify_source_binlog(self, checkpoint: CheckpointStore) -> None:
        """启动前校验源库 binlog 配置。不合规拒绝启动并给出可操作的提示。"""
        try:
            caps = self._reader.read_server_capabilities()
        except Exception as exc:
            raise TransientConnectionError(f"读取源库 binlog 配置失败: {exc}") from exc

        fmt = caps.get("format")
        row_image = caps.get("row_image")
        checkpoint.set_binlog_capabilities(fmt, row_image)

        if fmt is None:
            logger.warning(f"任务 {self.task_id} 无法读取 binlog_format，跳过校验")
            return

        if fmt != "ROW":
            raise SyncConfigError(
                f"源库 binlog_format 为 {fmt}，必须为 ROW 才能保证数据完整同步。"
                f"当前配置下 DML 语句会以 statement 形式记录而无法复制。"
                f"请在源库执行: SET GLOBAL binlog_format='ROW' 并写入配置文件使其持久化。"
            )

        if row_image and row_image != "FULL":
            raise SyncConfigError(
                f"源库 binlog_row_image 为 {row_image}，必须为 FULL。"
                f"MINIMAL 模式下 binlog 只记录主键，UPDATE 的 SET 子句会缺失字段。"
                f"请在源库执行: SET GLOBAL binlog_row_image='FULL'。"
            )

        row_metadata = caps.get("row_metadata")
        if row_metadata and row_metadata.upper() == "MINIMAL":
            # 不阻断：应用层会按列顺序还原列名。
            # 但要让用户知道为什么日志里会出现列名映射提示，
            # 以及为什么把 binlog_row_metadata 设为 FULL 更稳妥。
            logger.info(
                f"任务 {self.task_id} 源库 binlog_row_metadata=MINIMAL："
                "binlog 不含列名，同步时按列顺序还原。"
                "若期间修改过表结构，建议重设 FULL 或重建任务。"
            )

        logger.info(
            f"任务 {self.task_id} 源库 binlog 校验通过: format={fmt}, "
            f"row_image={row_image}, row_metadata={row_metadata}"
        )
