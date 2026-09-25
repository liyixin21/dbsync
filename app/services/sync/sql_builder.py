"""
行事件的 SQL 生成。

纯函数，不触碰数据库，因此可完整单元测试。

三条关键设计：

1. INSERT 一律带 `ON DUPLICATE KEY UPDATE`，使重放严格幂等。
   旧实现遇到主键冲突会把整表加入黑名单，导致该表静默停止同步。
2. UPDATE/DELETE 用主键定位，而非把所有列塞进 WHERE。
   旧实现的全列 WHERE 在浮点/时间戳精度差异下会匹配不到行，更新静默失效。
3. 无主键表退化为全列定位，并显式上报，由调用方记录告警。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class TableMeta:
    """目标表的写入元信息。"""

    schema: str
    name: str
    columns: Tuple[str, ...]                      # 按 ORDINAL_POSITION 排列的全部列
    pk_columns: Tuple[str, ...] = ()              # 主键列
    unique_keys: Tuple[Tuple[str, ...], ...] = ()  # 唯一键（元组列表）
    generated_columns: frozenset = field(default_factory=frozenset)  # 不可显式赋值

    @property
    def qualified_name(self) -> str:
        return f"`{self.schema}`.`{self.name}`"

    @property
    def locator_columns(self) -> Tuple[str, ...]:
        """
        行定位列。

        优先主键；无主键则取第一个唯一键；两者皆无时返回空元组，
        调用方回退到全列定位并告警。
        """
        if self.pk_columns:
            return self.pk_columns
        if self.unique_keys:
            return self.unique_keys[0]
        return ()

    @property
    def has_reliable_locator(self) -> bool:
        return bool(self.pk_columns or self.unique_keys)

    def writable_columns(self, keys: Sequence[str]) -> List[str]:
        """过滤出可显式赋值的列（排除生成列）。"""
        return [k for k in keys if k not in self.generated_columns]


@dataclass
class GeneratedSQL:
    """生成结果。`degraded` 为真时表示该表缺少可靠定位键。"""

    sql: str
    params: List[Any]
    degraded: bool = False
    reason: Optional[str] = None


def _quote(column: str) -> str:
    """反引号转义列名，内部反引号翻倍。"""
    return "`" + column.replace("`", "``") + "`"


def _assignments(columns: Sequence[str]) -> str:
    return ", ".join(f"{_quote(c)} = %s" for c in columns)


def _odku_clause(columns: Sequence[str]) -> str:
    """ON DUPLICATE KEY UPDATE 子句。"""
    return ", ".join(f"{_quote(c)} = VALUES({_quote(c)})" for c in columns)


def _where_by(columns: Sequence[str], values: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """按给定列构造 WHERE，NULL 用 IS NULL 处理。"""
    parts: List[str] = []
    params: List[Any] = []
    for col in columns:
        value = values.get(col)
        if value is None:
            parts.append(f"{_quote(col)} IS NULL")
        else:
            parts.append(f"{_quote(col)} = %s")
            params.append(value)
    if not parts:
        # 定位列为空属于调用方缺陷，绝不能生成恒真条件
        raise ValueError("定位列为空，拒绝生成无条件语句")
    return " AND ".join(parts), params


# ------------------------------------------------------------------ INSERT

def build_insert(meta: TableMeta, row: Dict[str, Any]) -> GeneratedSQL:
    """构造幂等 INSERT。"""
    if not row:
        raise ValueError("空行数据，拒绝生成 INSERT")

    columns = list(row.keys())
    columns = meta.writable_columns(columns)
    if not columns:
        raise ValueError(f"{meta.qualified_name} 无可写列（全部为生成列）")

    values = [row[c] for c in columns]
    col_sql = ", ".join(_quote(c) for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))

    update_targets = meta.writable_columns([c for c in columns if c not in meta.locator_columns])

    if update_targets:
        sql = (
            f"INSERT INTO {meta.qualified_name} ({col_sql}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {_odku_clause(update_targets)}"
        )
    else:
        # 整行都是定位列（例如全是主键的关联表）：冲突时无需更新任何字段
        first = update_targets[0] if update_targets else _first(columns)
        sql = (
            f"INSERT INTO {meta.qualified_name} ({col_sql}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {_quote(first)} = {_quote(first)}"
        )

    degraded = not meta.has_reliable_locator
    return GeneratedSQL(
        sql=sql,
        params=values,
        degraded=degraded,
        reason="表无主键/唯一键，冲突去重依赖 InnoDB 隐式键" if degraded else None,
    )


def _first(columns: Sequence[str]) -> str:
    if not columns:
        raise ValueError("列列表为空")
    return columns[0]


# ------------------------------------------------------------------ UPDATE

def build_update(meta: TableMeta, before: Dict[str, Any], after: Dict[str, Any]) -> GeneratedSQL:
    """
    构造 UPDATE：以行定位键匹配旧值，设置新值。

    主键本身被修改时，locator 取 before 中的旧主键（这是正确的目标行），
    SET 中包含新的主键值。
    """
    if not after:
        raise ValueError("更新后数据为空，拒绝生成 UPDATE")

    set_columns = meta.writable_columns(list(after.keys()))
    if not set_columns:
        raise ValueError(f"{meta.qualified_name} 无可更新列")

    locator = meta.locator_columns
    degraded = False
    reason = None

    if not locator:
        # 退化：用 before 的全列定位，配合 after 的补集
        locator = tuple(before.keys())
        degraded = True
        reason = "表无主键/唯一键，UPDATE 退化为全列匹配，精度敏感类型可能匹配失败"

    set_sql = _assignments(set_columns)
    where_columns = [c for c in locator if c in before]

    # 定位列在 before 中缺失时，用 after 的值兜底（例如只记录了部分行像）
    if not where_columns:
        where_columns = [c for c in locator if c in after]
        degraded = True
        reason = "binlog 行像缺少定位列，回退使用最新值匹配"

    where_sql, where_params = _where_by(where_columns, before if set(where_columns) <= set(before) else after)
    params = [after[c] for c in set_columns] + where_params

    sql = f"UPDATE {meta.qualified_name} SET {set_sql} WHERE {where_sql}"
    return GeneratedSQL(sql=sql, params=params, degraded=degraded, reason=reason)


# ------------------------------------------------------------------ DELETE

def build_delete(meta: TableMeta, row: Dict[str, Any]) -> GeneratedSQL:
    """构造 DELETE：以行定位键匹配。"""
    if not row:
        raise ValueError("空行数据，拒绝生成 DELETE")

    locator = meta.locator_columns
    degraded = False
    reason = None

    if not locator:
        locator = tuple(row.keys())
        degraded = True
        reason = "表无主键/唯一键，DELETE 退化为全列匹配，精度敏感类型可能匹配失败"

    where_columns = [c for c in locator if c in row]
    if not where_columns:
        raise ValueError(f"{meta.qualified_name} 行像不含任何定位列")

    where_sql, params = _where_by(where_columns, row)
    sql = f"DELETE FROM {meta.qualified_name} WHERE {where_sql}"
    return GeneratedSQL(sql=sql, params=params, degraded=degraded, reason=reason)
