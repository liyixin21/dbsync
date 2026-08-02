"""
FastAPI应用入口
"""
import os
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
from .core.database import async_init_db, SessionLocal
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
    """仅在没有任何用户时创建默认管理员账户（幂等，不会重置已有密码）"""
    from .models.database import User
    from passlib.context import CryptContext
    
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    db = SessionLocal()
    try:
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
            print("  请尽快通过 WebUI 修改默认密码！")
        else:
            # 已有用户，不做任何修改
            admin = db.query(User).filter(User.username == "admin").first()
            if admin is not None and admin.is_active == False:
                # 仅恢复被禁用但不想丢失密码的情况：提示但不自动修改
                print("管理员账户处于禁用状态，请在数据库中手动启用")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时初始化数据库
    await async_init_db()
    
    # 创建默认管理员
    create_default_admin()
    
    # 启动同步服务（添加异常处理回调）
    sync_task = asyncio.create_task(sync_service.start())
    sync_task.add_done_callback(
        lambda t: logger.error(f"同步服务异常退出: {t.exception()}") if t.exception() else None
    )
    
    # 启动备份服务（添加异常处理回调）
    backup_task = asyncio.create_task(backup_service.start())
    backup_task.add_done_callback(
        lambda t: logger.error(f"备份服务异常退出: {t.exception()}") if t.exception() else None
    )
    
    yield
    
    # 关闭时停止服务（处理 asyncio 取消异常）
    try:
        await sync_service.stop()
    except asyncio.CancelledError:
        pass
    try:
        await backup_service.stop()
    except asyncio.CancelledError:
        pass
    logger.info("应用已关闭")


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
    allow_credentials=False,
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