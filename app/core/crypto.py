"""
密码加密/解密工具
使用 Fernet 对称加密保护数据库密码
"""
import base64
import hashlib
from cryptography.fernet import Fernet
from .config import settings


def _derive_key(secret: str) -> bytes:
    """从 SECRET_KEY 派生 Fernet 密钥"""
    key = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(key)


_fernet = Fernet(_derive_key(settings.SECRET_KEY))


def encrypt(plaintext: str) -> str:
    """加密明文字符串，返回加密后的字符串"""
    if not plaintext:
        return plaintext
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """解密加密字符串，返回明文"""
    if not ciphertext:
        return ciphertext
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except Exception:
        # 如果解密失败，可能是旧的明文密码，直接返回
        return ciphertext


def is_encrypted(value: str) -> bool:
    """判断字符串是否已加密（Fernet token 以 gAAAAA 开头）"""
    if not value:
        return False
    try:
        _fernet.decrypt(value.encode())
        return True
    except Exception:
        return False
