"""
OpenList 上传配置的读写。

连接信息存于 SystemConfig 表；密码用与数据库凭据相同的 Fernet 加密，
不以明文落库。
"""
from typing import Optional

from loguru import logger

from ...core.config import settings
from ...core.crypto import CredentialDecryptError, decrypt, encrypt
from ...core.database import session_scope
from ...models.database import SystemConfig
from .client import OpenListClient, OpenListConfig

CONFIG_KEYS = {
    "enabled": "openlist_enabled",
    "base_url": "openlist_base_url",
    "username": "openlist_username",
    "password": "openlist_password",
    "remote_dir": "openlist_remote_dir",
    "verify_ssl": "openlist_verify_ssl",
}


def _read_all() -> dict:
    with session_scope() as db:
        rows = (
            db.query(SystemConfig)
            .filter(SystemConfig.key.in_(list(CONFIG_KEYS.values())))
            .all()
        )
        return {row.key: (row.value or "") for row in rows}


def _write(key: str, value: str, description: Optional[str] = None) -> None:
    with session_scope() as db:
        row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        if row is None:
            db.add(SystemConfig(key=key, value=value, description=description))
        else:
            row.value = value
            if description:
                row.description = description


def is_enabled() -> bool:
    return _read_all().get(CONFIG_KEYS["enabled"], "").lower() == "true"


def load_config(*, require_password: bool = True) -> Optional[OpenListConfig]:
    """
    读取配置。未启用或必填项缺失时返回 None。

    密码解密失败会抛出明确异常——静默返回空密码只会让上传以
    「登录被拒绝」收场，掩盖真正的原因（SECRET_KEY 变更）。
    """
    raw = _read_all()

    base_url = raw.get(CONFIG_KEYS["base_url"], "").strip()
    username = raw.get(CONFIG_KEYS["username"], "").strip()
    remote_dir = raw.get(CONFIG_KEYS["remote_dir"], "").strip() or "/"
    verify_ssl = raw.get(CONFIG_KEYS["verify_ssl"], "true").lower() != "false"

    if not base_url:
        return None
    if require_password and not username:
        return None

    password = ""
    stored = raw.get(CONFIG_KEYS["password"], "")
    if stored:
        try:
            password = decrypt(stored)
        except CredentialDecryptError as exc:
            raise RuntimeError(
                "OpenList 密码无法解密（SECRET_KEY 可能已变更），请在设置中重新填写"
            ) from exc

    if require_password and not password:
        return None

    return OpenListConfig(
        base_url=base_url,
        username=username,
        password=password,
        remote_dir=remote_dir,
        verify_ssl=verify_ssl,
    )


def save_config(
    *,
    enabled: bool,
    base_url: str,
    username: str,
    password: Optional[str],
    remote_dir: str,
    verify_ssl: bool,
) -> None:
    """
    保存配置。

    password 为 None 表示「不修改已保存的密码」，避免前端回显密码。
    """
    _write(CONFIG_KEYS["enabled"], "true" if enabled else "false", "OpenList 上传开关")
    _write(CONFIG_KEYS["base_url"], base_url.strip(), "OpenList 服务地址")
    _write(CONFIG_KEYS["username"], username.strip(), "OpenList 用户名")
    _write(CONFIG_KEYS["remote_dir"], remote_dir.strip() or "/", "OpenList 远程目录")
    _write(CONFIG_KEYS["verify_ssl"], "true" if verify_ssl else "false", "OpenList 校验 SSL 证书")

    if password:
        _write(CONFIG_KEYS["password"], encrypt(password), "OpenList 密码（加密存储）")


def describe_config() -> dict:
    """返回可供前端展示的配置（不含密码明文）。"""
    raw = _read_all()
    has_password = bool(raw.get(CONFIG_KEYS["password"], ""))
    return {
        "enabled": raw.get(CONFIG_KEYS["enabled"], "false").lower() == "true",
        "base_url": raw.get(CONFIG_KEYS["base_url"], ""),
        "username": raw.get(CONFIG_KEYS["username"], ""),
        "remote_dir": raw.get(CONFIG_KEYS["remote_dir"], "/"),
        "verify_ssl": raw.get(CONFIG_KEYS["verify_ssl"], "true").lower() != "false",
        "password_set": has_password,
    }


def build_client() -> Optional[OpenListClient]:
    """构造客户端。未配置时返回 None。"""
    config = load_config()
    if config is None:
        return None
    return OpenListClient(config)
