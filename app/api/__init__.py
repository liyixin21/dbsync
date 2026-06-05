"""
API路由包
"""
from fastapi import APIRouter

api_router = APIRouter()

# 导入各个路由模块
from .databases import router as databases_router
from .sync_tasks import router as sync_tasks_router
from .backup_plans import router as backup_plans_router
from .backup_history import router as backup_history_router
from .system import router as system_router
from .auth import router as auth_router
from .logs import router as logs_router

# 注册路由
api_router.include_router(auth_router, prefix="/auth", tags=["用户认证"])
api_router.include_router(databases_router, prefix="/databases", tags=["数据库管理"])
api_router.include_router(sync_tasks_router, prefix="/sync-tasks", tags=["同步任务"])
api_router.include_router(backup_plans_router, prefix="/backup-plans", tags=["备份计划"])
api_router.include_router(backup_history_router, prefix="/backup-history", tags=["备份历史"])
api_router.include_router(system_router, prefix="/system", tags=["系统管理"])
api_router.include_router(logs_router, prefix="/logs", tags=["日志管理"])