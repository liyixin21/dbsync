"""
日志API
"""
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel

from ..core.database import get_db
from ..core.config import now_beijing
from ..models.database import OperationLog, LoginLog, RunLog, User
from .auth import get_current_user

router = APIRouter()


class OperationLogResponse(BaseModel):
    """操作日志响应模型"""
    id: int
    user_id: Optional[int] = None
    username: Optional[str] = None
    action: str
    resource_type: Optional[str] = None
    resource_id: Optional[int] = None
    resource_name: Optional[str] = None
    detail: Optional[str] = None
    ip_address: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class LoginLogResponse(BaseModel):
    """登录日志响应模型"""
    id: int
    username: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    success: bool
    failure_reason: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class RunLogResponse(BaseModel):
    """运行日志响应模型"""
    id: int
    task_type: str
    task_id: int
    task_name: Optional[str] = None
    level: str
    message: str
    detail: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class OperationLogListResponse(BaseModel):
    """操作日志列表响应"""
    total: int
    items: List[OperationLogResponse]


class LoginLogListResponse(BaseModel):
    """登录日志列表响应"""
    total: int
    items: List[LoginLogResponse]


class RunLogListResponse(BaseModel):
    """运行日志列表响应"""
    total: int
    items: List[RunLogResponse]


@router.get("/operations", response_model=OperationLogListResponse)
async def list_operation_logs(
    username: Optional[str] = None,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    search: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取操作日志列表"""
    query = db.query(OperationLog)
    
    if search:
        # 全局搜索：匹配用户名、操作、资源类型、资源名称、详情
        query = query.filter(
            (OperationLog.username.contains(search)) |
            (OperationLog.action.contains(search)) |
            (OperationLog.resource_type.contains(search)) |
            (OperationLog.resource_name.contains(search)) |
            (OperationLog.detail.contains(search)) |
            (OperationLog.ip_address.contains(search))
        )
    if username:
        query = query.filter(OperationLog.username.contains(username))
    if action:
        query = query.filter(OperationLog.action.contains(action))
    if resource_type:
        query = query.filter(OperationLog.resource_type == resource_type)
    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            query = query.filter(OperationLog.created_at >= start_dt)
        except ValueError:
            pass
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            query = query.filter(OperationLog.created_at <= end_dt)
        except ValueError:
            pass
    
    total = query.count()
    items = query.order_by(OperationLog.created_at.desc()).offset(skip).limit(limit).all()
    
    return OperationLogListResponse(total=total, items=items)


@router.get("/logins", response_model=LoginLogListResponse)
async def list_login_logs(
    username: Optional[str] = None,
    success: Optional[bool] = None,
    search: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取登录日志列表"""
    query = db.query(LoginLog)
    
    if search:
        # 全局搜索：匹配用户名、IP、失败原因
        query = query.filter(
            (LoginLog.username.contains(search)) |
            (LoginLog.ip_address.contains(search)) |
            (LoginLog.failure_reason.contains(search))
        )
    if username:
        query = query.filter(LoginLog.username.contains(username))
    if success is not None:
        query = query.filter(LoginLog.success == success)
    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            query = query.filter(LoginLog.created_at >= start_dt)
        except ValueError:
            pass
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            query = query.filter(LoginLog.created_at <= end_dt)
        except ValueError:
            pass
    
    total = query.count()
    items = query.order_by(LoginLog.created_at.desc()).offset(skip).limit(limit).all()
    
    return LoginLogListResponse(total=total, items=items)


@router.get("/runs", response_model=RunLogListResponse)
async def list_run_logs(
    task_type: Optional[str] = None,
    task_id: Optional[int] = None,
    level: Optional[str] = None,
    search: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取运行日志列表"""
    query = db.query(RunLog)
    
    if search:
        # 全局搜索：匹配任务类型、任务名称、级别、消息、详情
        query = query.filter(
            (RunLog.task_type.contains(search)) |
            (RunLog.task_name.contains(search)) |
            (RunLog.level.contains(search)) |
            (RunLog.message.contains(search)) |
            (RunLog.detail.contains(search))
        )
    if task_type:
        query = query.filter(RunLog.task_type == task_type)
    if task_id:
        query = query.filter(RunLog.task_id == task_id)
    if level:
        query = query.filter(RunLog.level == level)
    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            query = query.filter(RunLog.created_at >= start_dt)
        except ValueError:
            pass
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            query = query.filter(RunLog.created_at <= end_dt)
        except ValueError:
            pass
    
    total = query.count()
    items = query.order_by(RunLog.created_at.desc()).offset(skip).limit(limit).all()
    
    return RunLogListResponse(total=total, items=items)


@router.get("/statistics")
async def get_log_statistics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取日志统计信息"""
    from sqlalchemy import func
    from datetime import timedelta
    
    now = now_beijing()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)
    
    # 操作日志统计
    total_operations = db.query(func.count(OperationLog.id)).scalar()
    today_operations = db.query(func.count(OperationLog.id)).filter(
        OperationLog.created_at >= today_start
    ).scalar()
    
    # 登录日志统计
    total_logins = db.query(func.count(LoginLog.id)).scalar()
    failed_logins = db.query(func.count(LoginLog.id)).filter(
        LoginLog.success == False
    ).scalar()
    today_logins = db.query(func.count(LoginLog.id)).filter(
        LoginLog.created_at >= today_start
    ).scalar()
    
    # 运行日志统计
    total_runs = db.query(func.count(RunLog.id)).scalar()
    error_runs = db.query(func.count(RunLog.id)).filter(
        RunLog.level == "ERROR"
    ).scalar()
    
    return {
        "operations": {
            "total": total_operations,
            "today": today_operations
        },
        "logins": {
            "total": total_logins,
            "failed": failed_logins,
            "today": today_logins
        },
        "runs": {
            "total": total_runs,
            "errors": error_runs
        }
    }


# ============================================================
# 删除操作日志
# ============================================================

@router.delete("/operations-clear")
async def clear_operation_logs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """清空所有操作日志"""
    count = db.query(OperationLog).delete()
    db.commit()
    return {"message": f"已清空 {count} 条操作日志"}


@router.delete("/operations/{log_id}")
async def delete_operation_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """删除单条操作日志"""
    log = db.query(OperationLog).filter(OperationLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="操作日志不存在")
    db.delete(log)
    db.commit()
    return {"message": "删除成功"}


# ============================================================
# 删除登录日志
# ============================================================

@router.delete("/logins-clear")
async def clear_login_logs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """清空所有登录日志"""
    count = db.query(LoginLog).delete()
    db.commit()
    return {"message": f"已清空 {count} 条登录日志"}


@router.delete("/logins/{log_id}")
async def delete_login_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """删除单条登录日志"""
    log = db.query(LoginLog).filter(LoginLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="登录日志不存在")
    db.delete(log)
    db.commit()
    return {"message": "删除成功"}


# ============================================================
# 删除运行日志
# ============================================================

@router.delete("/runs-clear")
async def clear_run_logs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """清空所有运行日志"""
    count = db.query(RunLog).delete()
    db.commit()
    return {"message": f"已清空 {count} 条运行日志"}


@router.delete("/runs/{log_id}")
async def delete_run_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """删除单条运行日志"""
    log = db.query(RunLog).filter(RunLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="运行日志不存在")
    db.delete(log)
    db.commit()
    return {"message": "删除成功"}