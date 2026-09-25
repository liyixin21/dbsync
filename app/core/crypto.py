"""
数据库凭据加解密。

使用 Fernet 对称加密，密钥由 SECRET_KEY 经 SHA-256 派生。
关键行为：解密失败时不再静默退回原文——那会把「密钥变更」伪装成「密码错误」，
而是区分「密钥变更导致的解密失败」与「历史遗留的明文凭据」两种情形。
"""
import base64
import hashlib
from typing import Iterable

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

from .config import settings

# Fernet token 的版本字节 0x80 经 base64url 编码后的固定前缀
_FERNET_PREFIX = "gAAAAA"


class CredentialDecryptError(Exception):
    """凭据解密失败（通常是 SECRET_KEY 变更或数据损坏）。"""


def _derive_key(secret: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


_fernet = Fernet(_derive_key(settings.SECRET_KEY))


def encrypt(plaintext: str) -> str:
    """加密明文凭据。空值原样返回。"""
    if not plaintext:
        return plaintext
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """
    解密凭据。

    合法 Fernet token 但解密失败 → 抛 CredentialDecryptError（密钥变更）。
    非 Fernet 格式 → 判定为历史明文凭据，原样返回并告警一次。
    """
    if not ciphertext:
        return ciphertext

    if not ciphertext.startswith(_FERNET_PREFIX):
        logger.warning("检测到未加密的数据库凭据，保存该记录时会自动加密")
        return ciphertext

    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise CredentialDecryptError(
            "数据库凭据解密失败：SECRET_KEY 已变更或凭据数据已损坏，请重新录入该数据库密码"
        ) from exc


def is_encrypted(value: str) -> bool:
    """判断字符串是否为当前密钥可解开的密文。"""
    if not value or not value.startswith(_FERNET_PREFIX):
        return False
    try:
        _fernet.decrypt(value.encode())
        return True
    except InvalidToken:
        return False


def self_check(values: Iterable[str]) -> int:
    """
    启动期自检：对所有存量凭据尝试解密。

    返回失败条数。调用方据此在启动日志中给出明确的修复指引，
    而不是等到用户点「启动同步」时才炸。
    """
    failed = 0
    for value in values:
        if not value or not value.startswith(_FERNET_PREFIX):
            continue
        try:
            _fernet.decrypt(value.encode())
        except InvalidToken:
            failed += 1
    return failed
