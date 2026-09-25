"""
表结构解析与缓存。

同步引擎需要知道每张表的主键列才能生成正确的 WHERE；
旧实现从不查询元数据，一律用全列匹配，这是 UPDATE 静默失效的根源。
"""
import threading
from typing import Dict, List, Optional, Tuple

import mysql.connector
from loguru import logger

from .tools import find_mysql_tool  # noqa: F401  (保持包导出完整)
from ..sync.sql_builder import TableMeta

_COLUMNS_SQL = """
    SELECT COLUMN_NAME, EXTRA
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
    ORDER BY ORDINAL_POSITION
"""

_STATISTICS_SQL = """
    SELECT INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX, NON_UNIQUE
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
    ORDER BY INDEX_NAME, SEQ_IN_INDEX
"""

_GENERATED_MARKERS = ("VIRTUAL GENERATED", "STORED GENERATED")


class SchemaResolver:
    """
    表结构缓存。线程安全：同一任务的消费线程与重连逻辑可能并发访问。

    **目标库映射**：binlog 事件携带的是源库的 schema 名，但写入必须落在
    目标库上。因此本类在构造时接收 target_schema，查询元数据与生成的
    TableMeta.schema 一律使用目标库名。

    若不做这层映射，同一实例上的「A 库 → B 库」同步会把 SQL 构造成
    `INSERT INTO A.t ...`，即原地写回源库，既污染数据又造成 binlog 无限循环。
    """

    def __init__(self, connection_provider, target_schema: str) -> None:
        self._provider = connection_provider
        self._target_schema = target_schema
        self._cache: Dict[Tuple[str, str], TableMeta] = {}
        self._lock = threading.RLock()
        self._warned: set = set()

    # ------------------------------------------------------------ 查询

    def _query(self, schema: str, table: str) -> Optional[TableMeta]:
        # 元数据一律从目标库查——列名/主键必须与写入目标一致
        meta_schema = self._target_schema
        conn = self._provider()
        cursor = conn.cursor()

        cursor.execute(_COLUMNS_SQL, (meta_schema, table))
        column_rows = cursor.fetchall()
        if not column_rows:
            cursor.close()
            return None

        columns: List[str] = []
        generated = set()
        for name, extra in column_rows:
            columns.append(name)
            if extra and any(m in extra.upper() for m in _GENERATED_MARKERS):
                generated.add(name)

        cursor.execute(_STATISTICS_SQL, (meta_schema, table))
        stats = cursor.fetchall()
        cursor.close()

        # 按索引名聚合列，保持 SEQ_IN_INDEX 顺序
        indexes: Dict[str, List[Tuple[int, str]]] = {}
        non_unique: Dict[str, bool] = {}
        for index_name, column_name, seq, is_non_unique in stats:
            indexes.setdefault(index_name, []).append((seq, column_name))
            non_unique[index_name] = bool(is_non_unique)

        pk_columns: Tuple[str, ...] = ()
        unique_keys: List[Tuple[str, ...]] = []
        for index_name, entries in indexes.items():
            ordered = tuple(col for _, col in sorted(entries))
            if index_name == "PRIMARY":
                pk_columns = ordered
            elif not non_unique.get(index_name, True):
                unique_keys.append(ordered)

        return TableMeta(
            schema=meta_schema,
            name=table,
            columns=tuple(columns),
            pk_columns=pk_columns,
            unique_keys=tuple(unique_keys),
            generated_columns=frozenset(generated),
        )

    # ------------------------------------------------------------ 公共接口

    def get(self, schema: str, table: str) -> Optional[TableMeta]:
        """取表元信息。表不存在返回 None。"""
        key = (schema, table)
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        try:
            meta = self._query(schema, table)
        except mysql.connector.Error as exc:
            logger.error(f"查询 {schema}.{table} 表结构失败: {exc}")
            return None

        if meta is None:
            return None

        with self._lock:
            self._cache[key] = meta

        if not meta.has_reliable_locator:
            self._warn_once(
                key,
                f"表 {schema}.{table} 无主键且无唯一键："
                "UPDATE/DELETE 将退化为全列匹配，精度敏感字段可能导致匹配失败，"
                "建议为该表添加主键",
            )
        return meta

    def invalidate(self, schema: str, table: Optional[str] = None) -> None:
        """DDL 事件后清除缓存。"""
        with self._lock:
            if table is None:
                for key in [k for k in self._cache if k[0] == schema]:
                    del self._cache[key]
                    self._warned.discard(key)
            else:
                key = (schema, table)
                self._cache.pop(key, None)
                self._warned.discard(key)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._warned.clear()

    def _warn_once(self, key: Tuple[str, str], message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(message)

    @property
    def cached_tables(self) -> int:
        with self._lock:
            return len(self._cache)
