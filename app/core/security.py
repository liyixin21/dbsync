"""
认证与安全原语：JWT、密码哈希、登录速率限制。

从原 api/auth.py 中抽出，使路由层只关心请求/响应编排。
"""
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

from jose import JWTError, jwt
from passlib.context import CryptContext

from .config import settings

ALGORITHM = "HS256"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """bcrypt 校验。对异常的哈希串返回 False 而不抛出。"""
    try:
        return pwd_context.verify(password, password_hash)
    except Exception:
        return False


def create_access_token(username: str, token_version: int = 0) -> str:
    """签发 JWT。携带 token_version 以支持改密/改名后主动失效旧令牌。"""
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {"sub": username, "ver": token_version, "exp": expire}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """解码 JWT。失败返回空字典，由调用方决定响应。"""
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return {}


class LoginRateLimiter:
    """
    进程内滑动窗口限流器，用于登录失败计数。

    单实例部署下足够；水平扩展时应换到 Redis 等共享后端。
    """

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self._max = max(1, max_attempts)
        self._window = max(1, window_seconds)
        self._hits: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def _live_hits(self, key: str, now: float) -> List[float]:
        hits = [t for t in self._hits.get(key, []) if now - t < self._window]
        self._hits[key] = hits
        return hits

    def check(self, key: str) -> Tuple[bool, int]:
        """返回 (是否允许本次尝试, 建议等待秒数)。"""
        with self._lock:
            now = time.monotonic()
            hits = self._live_hits(key, now)
            if len(hits) >= self._max:
                retry_after = int(self._window - (now - hits[0])) + 1
                return False, max(retry_after, 1)
            return True, 0

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._live_hits(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


login_rate_limiter = LoginRateLimiter(
    settings.LOGIN_MAX_ATTEMPTS, settings.LOGIN_WINDOW_SECONDS
)
