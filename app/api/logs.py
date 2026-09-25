"""
日志 API：操作日志、登录日志、运行日志的查询与清理。

修正旧实现的缺陷：logs.py 使用了未导入的 HTTPException，
删除不存在的记录时会抛 NameError 并返回 500。
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Query as SAQuery, Session

from ..core.config import now_beijing
from ..core.database import get_db
from ..core.errors import BadRequestError, NotFoundError
from ..core.pagination import Page, PageMeta, paginate
from ..models.database import LoginLog, OperationLog, RunLog, User
from ..services.audit import log_operation
from .deps import client_ip, get_current_user

router = APIRouter()


# ============================================================ 模型

class OperationLogResponse(BaseModel):
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

    model_config = {"from_attributes": True}


class LoginLogResponse(BaseModel):
    id: int
    username: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    success: bool
    failure_reason: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class RunLogResponse(BaseModel):
    id: int
    task_type: str
    task_id: int
    task_name: Optional[str] = None
    level: str
    message: str
    detail: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class LogStatisticsResponse(BaseModel):
    operations: dict
    logins: dict
    runs: dict


class MessageResponse(BaseModel):
    message: str
    affected: Optional[int] = None


# ============================================================ 辅助

def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _apply_dates(query: SAQuery, column, start: Optional[str], end: Optional[str]) -> SAQuery:
    start_dt = _parse_date(start)
    if start_dt:
        query = query.filter(column >= start_dt)
    end_dt = _parse_date(end)
    if end_dt:
        query = query.filter(column <= end_dt)
    return query


# ============================================================ 查询

@router.get("/operations", response_model=Page[OperationLogResponse])
async def list_operation_logs(
    search: Optional[str] = Query(default=None, max_length=200),
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Page[OperationLogResponse]:
    query = db.query(OperationLog)
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            OperationLog.username.like(pattern)
            | OperationLog.action.like(pattern)
            | OperationLog.resource_type.like(pattern)
            | OperationLog.resource_name.like(pattern)
            | OperationLog.detail.like(pattern)
            | OperationLog.ip_address.like(pattern)
        )
    if action:
        query = query.filter(OperationLog.action == action)
    if resource_type:
        query = query.filter(OperationLog.resource_type == resource_type)
    query = _apply_dates(query, OperationLog.created_at, start_date, end_date)
    query = query.order_by(OperationLog.id.desc())

    items, meta = paginate(query, skip, limit)
    return Page[OperationLogResponse](
        data=[OperationLogResponse.model_validate(row) for row in items], page=meta
    )


@router.get("/logins", response_model=Page[LoginLogResponse])
async def list_login_logs(
    search: Optional[str] = Query(default=None, max_length=200),
    success: Optional[bool] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Page[LoginLogResponse]:
    query = db.query(LoginLog)
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            LoginLog.username.like(pattern)
            | LoginLog.ip_address.like(pattern)
            | LoginLog.failure_reason.like(pattern)
        )
    if success is not None:
        query = query.filter(LoginLog.success == success)
    query = _apply_dates(query, LoginLog.created_at, start_date, end_date)
    query = query.order_by(LoginLog.id.desc())

    items, meta = paginate(query, skip, limit)
    return Page[LoginLogResponse](
        data=[LoginLogResponse.model_validate(row) for row in items], page=meta
    )


@router.get("/runs", response_model=Page[RunLogResponse])
async def list_run_logs(
    search: Optional[str] = Query(default=None, max_length=200),
    task_type: Optional[str] = None,
    task_id: Optional[int] = None,
    level: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Page[RunLogResponse]:
    query = db.query(RunLog)
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            RunLog.task_name.like(pattern)
            | RunLog.level.like(pattern)
            | RunLog.message.like(pattern)
            | RunLog.detail.like(pattern)
        )
    if task_type:
        query = query.filter(RunLog.task_type == task_type)
    if task_id is not None:
        query = query.filter(RunLog.task_id == task_id)
    if level:
        query = query.filter(RunLog.level == level.upper())
    query = _apply_dates(query, RunLog.created_at, start_date, end_date)
    query = query.order_by(RunLog.id.desc())

    items, meta = paginate(query, skip, limit)
    return Page[RunLogResponse](
        data=[RunLogResponse.model_validate(row) for row in items], page=meta
    )


@router.get("/statistics", response_model=LogStatisticsResponse)
async def get_log_statistics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LogStatisticsResponse:
    now = now_beijing()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    return LogStatisticsResponse(
        operations={
            "total": db.query(func.count(OperationLog.id)).scalar() or 0,
            "today": db.query(func.count(OperationLog.id))
            .filter(OperationLog.created_at >= today_start)
            .scalar()
            or 0,
        },
        logins={
            "total": db.query(func.count(LoginLog.id)).scalar() or 0,
            "failed": db.query(func.count(LoginLog.id))
            .filter(LoginLog.success == False)  # noqa: E712
            .scalar()
            or 0,
            "today": db.query(func.count(LoginLog.id))
            .filter(LoginLog.created_at >= today_start)
            .scalar()
            or 0,
        },
        runs={
            "total": db.query(func.count(RunLog.id)).scalar() or 0,
            "errors": db.query(func.count(RunLog.id))
            .filter(RunLog.level == "ERROR")
            .scalar()
            or 0,
        },
    )


# ============================================================ 清理
# 静态路径段（-clear）注册在参数化路径之前，避免被 /{log_id} 吞掉。

@router.delete("/operations-clear", response_model=MessageResponse)
async def clear_operation_logs(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    count = db.query(OperationLog).delete()
    log_operation(db, current_user, "清空操作日志", "log", None, None, f"{count} 条",
                  ip_address=client_ip(request))
    db.commit()
    return MessageResponse(message=f"已清空 {count} 条操作日志", affected=count)


@router.delete("/logins-clear", response_model=MessageResponse)
async def clear_login_logs(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    count = db.query(LoginLog).delete()
    log_operation(db, current_user, "清空登录日志", "log", None, None, f"{count} 条",
                  ip_address=client_ip(request))
    db.commit()
    return MessageResponse(message=f"已清空 {count} 条登录日志", affected=count)


@router.delete("/runs-clear", response_model=MessageResponse)
async def clear_run_logs(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    count = db.query(RunLog).delete()
    log_operation(db, current_user, "清空运行日志", "log", None, None, f"{count} 条",
                  ip_address=client_ip(request))
    db.commit()
    return MessageResponse(message=f"已清空 {count} 条运行日志", affected=count)


@router.delete("/operations/{log_id}", response_model=MessageResponse)
async def delete_operation_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    record = db.query(OperationLog).filter(OperationLog.id == log_id).first()
    if record is None:
        raise NotFoundError("操作日志不存在")
    db.delete(record)
    db.commit()
    return MessageResponse(message="删除成功", affected=1)


@router.delete("/logins/{log_id}", response_model=MessageResponse)
async def delete_login_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    record = db.query(LoginLog).filter(LoginLog.id == log_id).first()
    if record is None:
        raise NotFoundError("登录日志不存在")
    db.delete(record)
    db.commit()
    return MessageResponse(message="删除成功", affected=1)


@router.delete("/runs/{log_id}", response_model=MessageResponse)
async def delete_run_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    record = db.query(RunLog).filter(RunLog.id == log_id).first()
    if record is None:
        raise NotFoundError("运行日志不存在")
    db.delete(record)
    db.commit()
    return MessageResponse(message="删除成功", affected=1)
