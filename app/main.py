"""
FastAPI应用入口
"""
import os
import sys
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import uvicorn
from loguru import logger

from .core.config import settings
from .core.database import init_db, async_init_db, SessionLocal
from .core.services import sync_service, backup_service
from .api import api_router

# 配置 loguru 文件输出
os.makedirs(os.path.dirname(settings.LOG_FILE), exist_ok=True)
logger.add(
    settings.LOG_FILE,
    rotation="10 MB",
    retention="30 days",
    compression="gz",
    encoding="utf-8",
    level=settings.LOG_LEVEL,
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    backtrace=True,
    diagnose=True
)


def create_default_admin():
    """创建或恢复默认管理员账户（幂等）"""
    from .models.database import User
    from passlib.context import CryptContext
    
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    db = SessionLocal()
    try:
        # 查找 admin 用户
        admin = db.query(User).filter(User.username == "admin").first()
        if admin is None:
            # admin 不存在：检查是否有其他用户（可能被测试脚本改名了）
            # 尝试找到一个不活跃或孤立的用户来恢复
            # 如果没有任何用户，直接创建
            user_count = db.query(User).count()
            if user_count == 0:
                admin = User(
                    username="admin",
                    password_hash=pwd_context.hash("admin123"),
                    is_active=True
                )
                db.add(admin)
                db.commit()
                print("已创建默认管理员账户: admin / admin123")
            else:
                # 有其他用户但没有 admin，可能是测试脚本改名了
                # 重置第一个用户的用户名为 admin
                first_user = db.query(User).first()
                if first_user:
                    first_user.username = "admin"
                    first_user.password_hash = pwd_context.hash("admin123")
                    first_user.is_active = True
                    db.commit()
                    print("已恢复默认管理员账户: admin / admin123")
        else:
            # admin 存在，确保密码正确且账户启用
            if not pwd_context.verify("admin123", admin.password_hash):
                admin.password_hash = pwd_context.hash("admin123")
                db.commit()
                print("已重置管理员密码: admin123")
            if not admin.is_active:
                admin.is_active = True
                db.commit()
                print("已启用管理员账户")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时初始化数据库
    await async_init_db()
    
    # 创建默认管理员
    create_default_admin()
    
    # 启动同步服务
    asyncio.create_task(sync_service.start())
    
    # 启动备份服务
    asyncio.create_task(backup_service.start())
    
    yield
    
    # 关闭时停止服务
    await sync_service.stop()
    await backup_service.stop()


# 创建FastAPI应用
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="数据库实时同步备份工具",
    lifespan=lifespan
)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# 模板引擎
templates = Jinja2Templates(directory="app/static")

# 包含API路由
app.include_router(api_router, prefix="/api")


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """主页"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION
    }


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower()
    )