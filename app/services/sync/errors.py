"""
MySQL 服务端错误分类。

旧实现把 `Duplicate entry` 与 `doesn't exist` 一并当作「永久性错误」并把整表拉黑，
这是静默丢数据的根因。这里按可恢复性重新划分，每类对应明确处置动作。
"""
import enum
from typing import Optional, Tuple

from mysql.connector import errorcode


class ErrorClass(enum.Enum):
    """错误的处置类别。"""

    SCHEMA_MISSING = "schema_missing"
    """目标库缺表/缺列。可恢复：等待 DDL 事件同步过来后自动解除，不永久拉黑。"""

    DUPLICATE = "duplicate"
    """主键/唯一键冲突。应由幂等写入从源头消除；若仍出现说明目标库状态被外部改动。"""

    DATA = "data"
    """数据本身不被接受（超长、类型不匹配、非空约束）。该行无法写入，跳过但推进位点。"""

    CONNECTION = "connection"
    """网络/连接类。位点不推进，无限退避重连。"""

    AUTH = "auth"
    """权限或库不存在。属配置错误，需人工介入。"""

    FATAL = "fatal"
    """语法/结构类不可恢复错误，通常意味着代码缺陷。"""

    UNKNOWN = "unknown"


# MySQL 错误码 → 类别
_CODE_MAP = {
    errorcode.ER_NO_SUCH_TABLE: ErrorClass.SCHEMA_MISSING,            # 1146
    errorcode.ER_BAD_FIELD_ERROR: ErrorClass.SCHEMA_MISSING,          # 1054
    errorcode.ER_DUP_ENTRY: ErrorClass.DUPLICATE,                     # 1062
    errorcode.ER_DUP_KEY: ErrorClass.DUPLICATE,                       # 1022
    errorcode.ER_DATA_TOO_LONG: ErrorClass.DATA,                      # 1406
    errorcode.ER_TRUNCATED_WRONG_VALUE_FOR_FIELD: ErrorClass.DATA,    # 1366
    errorcode.ER_WARN_DATA_OUT_OF_RANGE: ErrorClass.DATA,             # 1264
    errorcode.ER_NO_DEFAULT_FOR_FIELD: ErrorClass.DATA,               # 1364
    errorcode.ER_BAD_NULL_ERROR: ErrorClass.DATA,                     # 1048
    errorcode.ER_NET_READ_ERROR: ErrorClass.CONNECTION,
    errorcode.ER_ACCESS_DENIED_ERROR: ErrorClass.AUTH,                # 1045
    errorcode.ER_BAD_DB_ERROR: ErrorClass.AUTH,                       # 1049
    errorcode.ER_DBACCESS_DENIED_ERROR: ErrorClass.AUTH,              # 1044
    errorcode.ER_PARSE_ERROR: ErrorClass.FATAL,                       # 1064
    errorcode.ER_BAD_FIELD_ERROR: ErrorClass.SCHEMA_MISSING,
}

# 部分客户端错误码未收录在 errorcode 常量中，按数值兜底
_EXTRA_CODES = {
    2006: ErrorClass.CONNECTION,   # CR_SERVER_GONE_ERROR
    2013: ErrorClass.CONNECTION,   # CR_SERVER_LOST
    2055: ErrorClass.CONNECTION,   # CR_SERVER_LOST_EXTENDED
    1927: ErrorClass.CONNECTION,   # 连接被 kill
}

# 文本兜底：部分异常（如 pymysql 包装过的）丢失了 errno
_TEXT_HINTS = [
    ("duplicate entry", ErrorClass.DUPLICATE),
    ("duplicate key", ErrorClass.DUPLICATE),
    ("doesn't exist", ErrorClass.SCHEMA_MISSING),
    ("does not exist", ErrorClass.SCHEMA_MISSING),
    ("unknown column", ErrorClass.SCHEMA_MISSING),
    ("gone away", ErrorClass.CONNECTION),
    ("lost connection", ErrorClass.CONNECTION),
    ("broken pipe", ErrorClass.CONNECTION),
    ("connection reset", ErrorClass.CONNECTION),
    ("timed out", ErrorClass.CONNECTION),
    ("access denied", ErrorClass.AUTH),
]


def extract_error_code(exc: BaseException) -> Optional[int]:
    """从异常中提取 MySQL 错误码。"""
    for attr in ("errno", "args"):
        value = getattr(exc, attr, None)
        if attr == "errno" and isinstance(value, int):
            return value
        if attr == "args" and value:
            first = value[0]
            if isinstance(first, int):
                return first
    return None


def classify_error(exc: BaseException) -> Tuple[ErrorClass, Optional[int]]:
    """判定错误的处置类别，返回 (类别, 错误码)。"""
    code = extract_error_code(exc)
    if code is not None:
        if code in _CODE_MAP:
            return _CODE_MAP[code], code
        if code in _EXTRA_CODES:
            return _EXTRA_CODES[code], code
        # 1xxx 多为服务端错误，2000+ 多为客户端连接错误
        if 2000 <= code < 3000:
            return ErrorClass.CONNECTION, code

    text = str(exc).lower()
    for hint, cls in _TEXT_HINTS:
        if hint in text:
            return cls, code

    return ErrorClass.UNKNOWN, code


# ---------------------------------------------------------------- 处置语义

def should_advance_checkpoint(cls: ErrorClass) -> bool:
    """
    该错误发生后是否仍应推进位点。

    - CONNECTION / AUTH：位点绝不能推进，否则重启后会从「已经跳过但没写成功」的位置开始，
      造成永久性数据缺口。
    - DATA / DUPLICATE / FATAL：事件本身无法应用，跳过它并记录到 DLQ，位点必须推进，
      否则会无限重试同一条坏数据而永久卡死。
    - SCHEMA_MISSING：位点推进（DDL 会随后到达），表标记为等待重建。
    """
    return cls in (
        ErrorClass.DATA,
        ErrorClass.DUPLICATE,
        ErrorClass.FATAL,
        ErrorClass.SCHEMA_MISSING,
        ErrorClass.UNKNOWN,
    )


def should_retry(cls: ErrorClass) -> bool:
    """是否值得在同一事务内重试一次。"""
    return cls in (ErrorClass.CONNECTION, ErrorClass.UNKNOWN)


def to_dlq(cls: ErrorClass) -> bool:
    """
    是否应记入失败事件队列。

    SCHEMA_MISSING 也要记：这些事件确实没能落到目标库，属于真实的数据缺口。
    把它们留下并让表状态标红，运维才知道「目标库缺表期间丢了多少行」，
    并且可以在手工建表后选择性重放。
    去重由 Applier 负责，不会按事件数量刷屏。
    """
    return cls in (
        ErrorClass.DATA,
        ErrorClass.DUPLICATE,
        ErrorClass.FATAL,
        ErrorClass.SCHEMA_MISSING,
        ErrorClass.UNKNOWN,
    )


def mark_table_broken(cls: ErrorClass) -> bool:
    """
    是否把该表标记为需要人工/DDL 介入。

    注意：DUPLICATE 与 DATA 都**不**触发——它们只影响单行，不该让整张表停摆。
    """
    return cls is ErrorClass.SCHEMA_MISSING
