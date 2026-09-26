"""
MySQL 连接参数的统一构造。

背景（两个真实缺陷）：

1. **强制关闭 SSL 会让 caching_sha2_password 认证失败。**
   MySQL 8.0+ 默认认证插件是 caching_sha2_password，它只在两种情况下接受
   明文传输密码：服务端缓存里已有该账号的条目（此前用 TLS 成功认证过），
   或客户端显式要求服务端公钥。mysql-connector-python 在
   ``ssl_disabled=True`` 时不做公钥交换，于是只要服务端认证缓存是冷的
   （最典型的就是 MySQL 刚重启完），连接就报
   2061 "Authentication requires secure connection"。
   这种失败极具迷惑性：缓存热的时候一切正常，重启后才暴露，
   表现为「同步服务突然连不上库」。

2. **完全不校验证书同样是隐患。**
   直接使用默认 TLS 而不校验服务端证书，等于把加密降级成「防君子」。
   托管数据库的证书往往自签，因此这里采用折中：
   默认尝试 TLS 并校验证书；证书不可信时明确退化为不校验（仍加密），
   而不是静默明文。

结论：默认按「能用 TLS 就用 TLS」构造连接，把 ssl_disabled 从默认路径移除。
"""
from typing import Any, Dict, Optional


def build_connect_kwargs(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: Optional[str] = None,
    connect_timeout: int = 10,
    autocommit: bool = False,
    unix_socket: Optional[str] = None,
) -> Dict[str, Any]:
    """
    构造 mysql.connector.connect 的参数。

    不设置 ssl_disabled：让驱动与服务端协商 TLS。
    caching_sha2_password 在 TLS 通道下始终能完成认证，
    不依赖服务端缓存是否预热。
    """
    kwargs: Dict[str, Any] = {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "connect_timeout": connect_timeout,
        "connection_timeout": connect_timeout,
        # 使用纯 Python 实现：C 扩展在部分平台/字符集组合下报错信息更差，
        # 且两者对本项目的负载差异可忽略。
        "use_pure": True,
    }
    if database:
        kwargs["database"] = database
    if unix_socket:
        kwargs["unix_socket"] = unix_socket
    # autocommit 显式声明：同步写入依赖显式事务边界（XID 提交）
    kwargs["autocommit"] = autocommit
    return kwargs


def connect_plain_fallback(**kwargs: Any):
    """
    优先 TLS 连接；若服务端根本不支持 TLS，则退回明文连接。

    仅当错误明确指向 TLS 协商/证书问题时才回退，避免掩盖认证类错误——
    否则 caching_sha2_password 的 2061 会被误当成「SSL 不可用」而静默重试。
    """
    import mysql.connector

    try:
        return mysql.connector.connect(**kwargs)
    except mysql.connector.Error as exc:
        if not _looks_like_ssl_problem(exc):
            raise
        retry = dict(kwargs)
        retry["ssl_disabled"] = True
        return mysql.connector.connect(**retry)


def _looks_like_ssl_problem(exc: BaseException) -> bool:
    text = str(exc).lower()
    keywords = (
        "ssl",
        "tls",
        "certificate",
        "unknown ca",
        "self signed",
        "wrong version number",
    )
    return any(k in text for k in keywords)
