"""
用户认证API
"""
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from pydantic import BaseModel
from jose import JWTError, jwt
from passlib.context import CryptContext

from ..core.database import get_db
from ..core.config import settings, now_beijing
from ..models.database import User, LoginLog, OperationLog

router = APIRouter()
security = HTTPBearer()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT 配置
ALGORITHM = "HS256"


class LoginRequest(BaseModel):
    """登录请求模型"""
    username: str
    password: str


class LoginResponse(BaseModel):
    """登录响应模型"""
    access_token: str
    token_type: str = "bearer"
    username: str


class UserCreate(BaseModel):
    """创建用户请求模型"""
    username: str
    password: str


class ChangeUsernameRequest(BaseModel):
    """修改用户名请求模型"""
    password: str
    new_username: str


class ChangePasswordRequest(BaseModel):
    """修改密码请求模型"""
    old_password: str
    new_password: str


class UserResponse(BaseModel):
    """用户响应模型"""
    id: int
    username: str
    is_active: bool
    created_at: datetime
    last_login: Optional[datetime] = None
    
    class Config:
        from_attributes = True


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None, token_version: int = 0):
    """创建 JWT token，包含令牌版本号用于支持主动失效"""
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "ver": token_version})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> dict:
    """验证 JWT token"""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="无效的认证凭据")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """获取当前认证用户，同时校验令牌版本号"""
    payload = verify_token(credentials.credentials)
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="无效的认证凭据")
    
    user = db.query(User).filter(User.username == username).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已禁用")
    
    # 校验令牌版本号（用户名/密码变更后旧令牌自动失效）
    token_ver = payload.get("ver", 0)
    if token_ver != (user.token_version or 0):
        raise HTTPException(status_code=401, detail="令牌已失效，请重新登录")
    
    return user


def log_operation(db: Session, user: User, action: str, resource_type: str = None, 
                  resource_id: int = None, resource_name: str = None, 
                  detail: str = None, ip_address: str = None):
    """记录操作日志"""
    log = OperationLog(
        user_id=user.id,
        username=user.username,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_name=resource_name,
        detail=detail,
        ip_address=ip_address
    )
    db.add(log)
    db.commit()


@router.post("/login", response_model=LoginResponse)
async def login(request: Request, login_data: LoginRequest, db: Session = Depends(get_db)):
    """用户登录"""
    ip_address = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "")
    
    # 查找用户
    user = db.query(User).filter(User.username == login_data.username).first()
    
    # 验证密码
    if not user or not pwd_context.verify(login_data.password, user.password_hash):
        # 记录失败登录
        login_log = LoginLog(
            username=login_data.username,
            ip_address=ip_address,
            user_agent=user_agent,
            success=False,
            failure_reason="用户名或密码错误"
        )
        db.add(login_log)
        db.commit()
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    
    if not user.is_active:
        login_log = LoginLog(
            username=login_data.username,
            ip_address=ip_address,
            user_agent=user_agent,
            success=False,
            failure_reason="账户已禁用"
        )
        db.add(login_log)
        db.commit()
        raise HTTPException(status_code=403, detail="账户已禁用")
    
    # 创建 token（包含令牌版本号）
    access_token = create_access_token(data={"sub": user.username}, token_version=user.token_version or 0)
    
    # 更新最后登录时间
    user.last_login = now_beijing()
    
    # 记录成功登录
    login_log = LoginLog(
        username=user.username,
        ip_address=ip_address,
        user_agent=user_agent,
        success=True
    )
    db.add(login_log)
    db.commit()
    
    return LoginResponse(access_token=access_token, username=user.username)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """获取当前用户信息"""
    return current_user


@router.post("/users", response_model=UserResponse)
async def create_user(
    user_data: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """创建用户（需要认证）"""
    # 检查用户名是否已存在
    existing = db.query(User).filter(User.username == user_data.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")
    
    # 创建用户
    user = User(
        username=user_data.username,
        password_hash=pwd_context.hash(user_data.password)
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    log_operation(db, current_user, "创建用户", "user", user.id, user.username)
    
    return user


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取用户列表"""
    return db.query(User).all()


@router.put("/users/{user_id}/toggle")
async def toggle_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """启用/禁用用户"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    user.is_active = not user.is_active
    db.commit()
    
    action = "启用用户" if user.is_active else "禁用用户"
    log_operation(db, current_user, action, "user", user.id, user.username)
    
    return {"message": f"用户已{'启用' if user.is_active else '禁用'}"}


@router.put("/change-username")
async def change_username(
    data: ChangeUsernameRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """修改当前用户用户名"""
    # 验证密码
    if not pwd_context.verify(data.password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="密码错误")
    
    # 检查新用户名是否已存在
    if len(data.new_username) < 3:
        raise HTTPException(status_code=400, detail="用户名至少3个字符")
    
    existing = db.query(User).filter(User.username == data.new_username).first()
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")
    
    old_username = current_user.username
    current_user.username = data.new_username
    current_user.token_version = (current_user.token_version or 0) + 1
    db.commit()
    
    log_operation(db, current_user, "修改用户名", "user", current_user.id, 
                  f"{old_username} -> {data.new_username}")
    
    # 生成新token（使用新的令牌版本号）
    access_token = create_access_token(data={"sub": data.new_username}, token_version=current_user.token_version)
    
    return {"message": "用户名修改成功", "access_token": access_token, "username": data.new_username}


@router.put("/change-password")
async def change_password(
    data: ChangePasswordRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """修改当前用户密码"""
    # 验证旧密码
    if not pwd_context.verify(data.old_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码错误")
    
    # 检查新密码长度
    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少6个字符")
    
    current_user.password_hash = pwd_context.hash(data.new_password)
    current_user.token_version = (current_user.token_version or 0) + 1
    db.commit()
    
    log_operation(db, current_user, "修改密码", "user", current_user.id, current_user.username)
    
    # 生成新token（旧令牌因版本号不匹配将自动失效）
    access_token = create_access_token(data={"sub": current_user.username}, token_version=current_user.token_version)
    
    return {"message": "密码修改成功", "access_token": access_token}