"""
用户认证 API。

相比旧实现新增登录失败限流：旧代码记录了失败登录却从不据此决策，
密码可被无限次暴力尝试。
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.config import now_beijing, settings
from ..core.database import get_db
from ..core.errors import AuthError, BadRequestError, ConflictError, RateLimitError
from ..core.security import (
    create_access_token,
    hash_password,
    login_rate_limiter,
    verify_password,
)
from ..models.database import User
from ..services.audit import log_login, log_operation
from .deps import client_ip, get_current_user

router = APIRouter()


# ============================================================ 模型

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    expires_in: int


class UserResponse(BaseModel):
    id: int
    username: str
    is_active: bool
    created_at: datetime
    last_login: Optional[datetime] = None

    model_config = {"from_attributes": True}


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=6, max_length=200)


class ChangeUsernameRequest(BaseModel):
    password: str
    new_username: str = Field(min_length=3, max_length=50)


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=200)


class TokenResponse(BaseModel):
    message: str
    access_token: Optional[str] = None
    username: Optional[str] = None


class MessageResponse(BaseModel):
    message: str


# ============================================================ 登录

@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> LoginResponse:
    """用户登录。连续失败超过阈值触发限流。"""
    ip = client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    limiter_key = f"{payload.username}@{ip}"

    allowed, retry_after = login_rate_limiter.check(limiter_key)
    if not allowed:
        log_login(
            db, payload.username, ip, user_agent, False,
            f"登录过于频繁（{retry_after}s 后重试）",
        )
        db.commit()
        raise RateLimitError(f"登录尝试过于频繁，请 {retry_after} 秒后重试")

    user = db.query(User).filter(User.username == payload.username).first()

    # 无论用户是否存在都执行一次哈希校验，避免通过响应时间区分账号是否存在
    password_ok = verify_password(payload.password, user.password_hash) if user else False
    if user is None:
        hash_password(payload.password)  # 拉平时序

    if not password_ok:
        login_rate_limiter.record_failure(limiter_key)
        log_login(db, payload.username, ip, user_agent, False, "用户名或密码错误")
        db.commit()
        raise AuthError("用户名或密码错误")

    if not user.is_active:
        log_login(db, payload.username, ip, user_agent, False, "账户已禁用")
        db.commit()
        raise BadRequestError("账户已被禁用")

    login_rate_limiter.reset(limiter_key)

    token = create_access_token(user.username, user.token_version or 0)
    user.last_login = now_beijing()
    log_login(db, user.username, ip, user_agent, True)
    db.commit()

    return LoginResponse(
        access_token=token,
        username=user.username,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)) -> User:
    """当前登录用户。"""
    return current_user


# ============================================================ 用户管理

@router.get("/users", response_model=List[UserResponse])
async def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[User]:
    return db.query(User).order_by(User.id).all()


@router.post("/users", response_model=UserResponse)
async def create_user(
    payload: UserCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> User:
    """创建用户。"""
    existing = db.query(User).filter(User.username == payload.username).first()
    if existing is not None:
        raise ConflictError("用户名已存在")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.flush()

    log_operation(
        db, current_user, "创建用户", "user", user.id, user.username,
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(user)
    return user


@router.put("/users/{user_id}/toggle", response_model=MessageResponse)
async def toggle_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """启用/禁用用户。"""
    if user_id == current_user.id:
        raise BadRequestError("不能禁用当前登录账户")

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        from ..core.errors import NotFoundError

        raise NotFoundError("用户不存在")

    user.is_active = not user.is_active
    if not user.is_active:
        # 禁用后立即使其所有已签发令牌失效
        user.token_version = (user.token_version or 0) + 1

    action = "启用用户" if user.is_active else "禁用用户"
    log_operation(
        db, current_user, action, "user", user.id, user.username,
        ip_address=client_ip(request),
    )
    db.commit()
    return MessageResponse(message=f"用户已{action[2:]}")


# ============================================================ 凭据变更

@router.put("/change-username", response_model=TokenResponse)
async def change_username(
    payload: ChangeUsernameRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TokenResponse:
    """修改用户名。成功后旧令牌失效并返回新令牌。"""
    if not verify_password(payload.password, current_user.password_hash):
        raise BadRequestError("密码错误")

    if payload.new_username == current_user.username:
        raise BadRequestError("新用户名与当前用户名相同")

    existing = (
        db.query(User)
        .filter(User.username == payload.new_username, User.id != current_user.id)
        .first()
    )
    if existing is not None:
        raise ConflictError("用户名已被占用")

    old_username = current_user.username
    current_user.username = payload.new_username
    current_user.token_version = (current_user.token_version or 0) + 1

    log_operation(
        db, current_user, "修改用户名", "user", current_user.id, payload.new_username,
        f"{old_username} → {payload.new_username}", ip_address=client_ip(request),
    )
    db.commit()

    token = create_access_token(current_user.username, current_user.token_version)
    return TokenResponse(
        message="用户名修改成功", access_token=token, username=current_user.username
    )


@router.put("/change-password", response_model=TokenResponse)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TokenResponse:
    """修改密码。旧令牌因版本号不匹配自动失效。"""
    if not verify_password(payload.old_password, current_user.password_hash):
        raise BadRequestError("旧密码错误")

    if payload.old_password == payload.new_password:
        raise BadRequestError("新密码不能与旧密码相同")

    current_user.password_hash = hash_password(payload.new_password)
    current_user.token_version = (current_user.token_version or 0) + 1

    log_operation(
        db, current_user, "修改密码", "user", current_user.id, current_user.username,
        ip_address=client_ip(request),
    )
    db.commit()

    token = create_access_token(current_user.username, current_user.token_version)
    return TokenResponse(
        message="密码修改成功", access_token=token, username=current_user.username
    )
