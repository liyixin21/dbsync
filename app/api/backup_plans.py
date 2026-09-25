"""
备份计划 API。

仅支持全量备份。相比旧实现：
- 调度交给统一的 APScheduler，不再每个计划一个忙轮询线程
- 删除计划时一并删除备份文件，避免孤儿文件占满磁盘
- 立即执行改为异步子进程，不再冻结事件循环
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..core.errors import BadRequestError, NameTakenError, NotFoundError
from ..models.database import BackupPlan, BackupType, Database, User
from ..services.audit import log_operation
from ..services.backup import backup_manager
from ..services.backup.engine import delete_backup_file
from .deps import client_ip, get_current_user

router = APIRouter()


# ============================================================ 模型

class BackupPlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    database_id: int
    schedule_interval: int = Field(ge=1, le=100000, description="备份间隔(分钟)")
    retention_count: int = Field(default=50, ge=1, le=10000)
    is_active: bool = True
    upload_enabled: bool = Field(default=False, description="备份完成后上传到 OpenList")
    upload_dir: Optional[str] = Field(
        default=None, max_length=500, description="远程目录，留空用全局配置"
    )


class BackupPlanUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    database_id: Optional[int] = None
    schedule_interval: Optional[int] = Field(default=None, ge=1, le=100000)
    retention_count: Optional[int] = Field(default=None, ge=1, le=10000)
    is_active: Optional[bool] = None
    upload_enabled: Optional[bool] = None
    upload_dir: Optional[str] = Field(default=None, max_length=500)


class BackupPlanResponse(BaseModel):
    id: int
    name: str
    database_id: int
    database_name: Optional[str] = None
    backup_type: str
    schedule_interval: int
    retention_count: int
    is_active: bool
    running: bool = False
    upload_enabled: bool = False
    upload_dir: Optional[str] = None
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class ExecuteResponse(BaseModel):
    message: str
    plan_id: int
    history_id: Optional[int] = None


class MessageResponse(BaseModel):
    message: str
    deleted_files: Optional[int] = None


# ============================================================ 辅助

def _to_response(plan: BackupPlan, db: Session) -> BackupPlanResponse:
    database = db.query(Database).filter(Database.id == plan.database_id).first()
    return BackupPlanResponse(
        id=plan.id,
        name=plan.name,
        database_id=plan.database_id,
        database_name=database.name if database else None,
        backup_type=plan.backup_type.value if plan.backup_type else "full",
        schedule_interval=plan.schedule_interval,
        retention_count=plan.retention_count,
        is_active=plan.is_active,
        running=backup_manager.is_inflight(plan.id),
        upload_enabled=bool(plan.upload_enabled),
        upload_dir=plan.upload_dir,
        last_run_at=plan.last_run_at,
        next_run_at=plan.next_run_at,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


def _get_plan(db: Session, plan_id: int) -> BackupPlan:
    plan = db.query(BackupPlan).filter(BackupPlan.id == plan_id).first()
    if plan is None:
        raise NotFoundError("备份计划不存在")
    return plan


def _assert_name_free(db: Session, name: str, exclude_id: Optional[int] = None) -> None:
    query = db.query(BackupPlan).filter(BackupPlan.name == name)
    if exclude_id is not None:
        query = query.filter(BackupPlan.id != exclude_id)
    if query.first() is not None:
        raise NameTakenError(f"计划名称「{name}」已存在")


# ============================================================ 端点

@router.get("/", response_model=List[BackupPlanResponse])
async def list_backup_plans(
    skip: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[BackupPlanResponse]:
    plans = (
        db.query(BackupPlan)
        .order_by(BackupPlan.id)
        .offset(max(0, skip))
        .limit(max(1, min(limit, 500)))
        .all()
    )
    return [_to_response(plan, db) for plan in plans]


@router.post("/", response_model=BackupPlanResponse)
async def create_backup_plan(
    payload: BackupPlanCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BackupPlanResponse:
    """创建全量备份计划。启用状态下立即排程。"""
    if db.query(Database).filter(Database.id == payload.database_id).first() is None:
        raise BadRequestError("数据库不存在")

    _assert_name_free(db, payload.name)

    plan = BackupPlan(
        name=payload.name,
        database_id=payload.database_id,
        backup_type=BackupType.FULL,
        schedule_interval=payload.schedule_interval,
        retention_count=payload.retention_count,
        is_active=payload.is_active,
        upload_enabled=payload.upload_enabled,
        upload_dir=(payload.upload_dir or "").strip() or None,
    )
    db.add(plan)
    db.flush()

    log_operation(
        db, current_user, "创建备份计划", "backup_plan", plan.id, plan.name,
        f"每 {plan.schedule_interval} 分钟，保留 {plan.retention_count} 份",
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(plan)

    backup_manager.on_plan_created(plan.id, plan.is_active, plan.schedule_interval)
    db.refresh(plan)
    return _to_response(plan, db)


@router.get("/{plan_id}", response_model=BackupPlanResponse)
async def get_backup_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BackupPlanResponse:
    return _to_response(_get_plan(db, plan_id), db)


@router.put("/{plan_id}", response_model=BackupPlanResponse)
async def update_backup_plan(
    plan_id: int,
    payload: BackupPlanUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BackupPlanResponse:
    """更新备份计划。启用状态或间隔变化时重排调度。"""
    plan = _get_plan(db, plan_id)
    old_active = plan.is_active
    old_interval = plan.schedule_interval

    if payload.name is not None:
        _assert_name_free(db, payload.name, exclude_id=plan_id)
        plan.name = payload.name

    if payload.database_id is not None:
        if db.query(Database).filter(Database.id == payload.database_id).first() is None:
            raise BadRequestError("数据库不存在")
        plan.database_id = payload.database_id

    if payload.schedule_interval is not None:
        plan.schedule_interval = payload.schedule_interval
    if payload.retention_count is not None:
        plan.retention_count = payload.retention_count
    if payload.is_active is not None:
        plan.is_active = payload.is_active
    if payload.upload_enabled is not None:
        plan.upload_enabled = payload.upload_enabled
    if payload.upload_dir is not None:
        plan.upload_dir = payload.upload_dir.strip() or None

    log_operation(db, current_user, "更新备份计划", "backup_plan", plan.id, plan.name,
                  ip_address=client_ip(request))
    db.commit()
    db.refresh(plan)

    schedule_changed = old_interval != plan.schedule_interval
    backup_manager.on_plan_updated(
        plan.id, plan.is_active, plan.schedule_interval, schedule_changed
    )
    if old_active != plan.is_active:
        log_operation(
            db, current_user,
            "启用备份计划" if plan.is_active else "禁用备份计划",
            "backup_plan", plan.id, plan.name, ip_address=client_ip(request),
        )
        db.commit()

    db.refresh(plan)
    return _to_response(plan, db)


@router.delete("/{plan_id}", response_model=MessageResponse)
async def delete_backup_plan(
    plan_id: int,
    request: Request,
    delete_files: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """
    删除备份计划。

    默认连同备份文件一起删除——旧实现只删记录不删文件，磁盘上会积累大量
    再也无法从界面管理到的孤儿 .sql 文件。
    """
    from ..models.database import BackupHistory

    plan = _get_plan(db, plan_id)
    name = plan.name

    backup_manager.on_plan_deleted(plan_id)

    removed_files = 0
    histories = db.query(BackupHistory).filter(BackupHistory.backup_plan_id == plan_id).all()
    if delete_files:
        for history in histories:
            ok, _ = delete_backup_file(history.file_path)
            if ok and history.file_path:
                removed_files += 1

    for history in histories:
        db.delete(history)
    db.delete(plan)

    log_operation(
        db, current_user, "删除备份计划", "backup_plan", plan_id, name,
        f"同时删除 {removed_files} 个备份文件" if delete_files else "保留备份文件",
        ip_address=client_ip(request),
    )
    db.commit()
    return MessageResponse(message="删除成功", deleted_files=removed_files)


@router.post("/{plan_id}/execute", response_model=ExecuteResponse)
async def execute_backup_plan(
    plan_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExecuteResponse:
    """立即执行一次全量备份。"""
    plan = _get_plan(db, plan_id)

    history_id = await backup_manager.execute_now(plan_id)

    log_operation(
        db, current_user, "手动执行备份", "backup_plan", plan_id, plan.name,
        f"历史记录 #{history_id}", ip_address=client_ip(request),
    )
    db.commit()
    return ExecuteResponse(message="备份已完成", plan_id=plan_id, history_id=history_id)
