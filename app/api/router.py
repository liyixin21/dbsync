"""
路由装配。所有业务路由挂在 /api 下。
"""
from fastapi import APIRouter

from . import auth, backup_history, backup_plans, databases, logs, sync_tasks, system

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(databases.router, prefix="/databases", tags=["数据库管理"])
api_router.include_router(sync_tasks.router, prefix="/sync-tasks", tags=["同步任务"])
api_router.include_router(backup_plans.router, prefix="/backup-plans", tags=["备份计划"])
api_router.include_router(backup_history.router, prefix="/backup-history", tags=["备份历史"])
api_router.include_router(system.router, prefix="/system", tags=["系统"])
api_router.include_router(logs.router, prefix="/logs", tags=["日志"])
