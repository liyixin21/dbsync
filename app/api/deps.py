"""
公共依赖：认证、分页参数、客户端 IP。
"""
from typing import Optional

from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..core.errors import AuthError, TokenExpiredError
from ..core.security import decode_token
from ..models.database import User

# auto_error=False：认证失败时由我们抛出统一格式的 AppError，
# 而不是 Starlette 默认的 {"detail": "Not authenticated"}
_bearer = HTTPBearer(auto_error=False)


def client_ip(request: Request) -> str:
    """取客户端 IP。信任反向代理注入的 X-Forwarded-For 首段。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """解析并校验当前用户。同时校验令牌版本号。"""
    if credentials is None or not credentials.credentials:
        raise AuthError("未提供认证凭据")

    payload = decode_token(credentials.credentials)
    if not payload:
        raise TokenExpiredError("认证凭据无效或已过期")

    username = payload.get("sub")
    if not username:
        raise TokenExpiredError("认证凭据无效")

    user = db.query(User).filter(User.username == username).first()
    if user is None or not user.is_active:
        raise AuthError("用户不存在或已被禁用")

    if payload.get("ver", 0) != (user.token_version or 0):
        raise TokenExpiredError("令牌已失效，请重新登录")

    return user


class PageParams:
    """统一分页参数。"""

    def __init__(
        self,
        skip: int = Query(0, ge=0, description="偏移量"),
        limit: int = Query(50, ge=1, le=200, description="每页数量"),
    ) -> None:
        self.skip = skip
        self.limit = limit
