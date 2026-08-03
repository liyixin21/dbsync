"""
备份计划API
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from ..core.database import get_db
from ..core.services import backup_service
from ..models.database import BackupPlan, BackupType, Database, User
from .auth import get_current_user, log_operation

router = APIRouter()


class BackupPlanCreate(BaseModel):
    """创建备份计划请求模型"""
    name: str
    database_id: int
    backup_type: str = "full"
    schedule_interval: Optional[int] = None
    retention_count: int = 50
    is_active: bool = True


class BackupPlanUpdate(BaseModel):
    """更新备份计划请求模型"""
    name: Optional[str] = None
    database_id: Optional[int] = None
    backup_type: Optional[str] = None
    schedule_interval: Optional[int] = None
    retention_count: Optional[int] = None
    is_active: Optional[bool] = None


class BackupPlanResponse(BaseModel):
    """备份计划响应模型"""
    id: int
    name: str
    database_id: int
    backup_type: str = "full"
    schedule_interval: Optional[int] = None
    retention_count: Optional[int] = 50
    is_active: bool = True
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


@router.get("/", response_model=List[BackupPlanResponse])
async def list_backup_plans(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取备份计划列表"""
    plans = db.query(BackupPlan).offset(skip).limit(limit).all()
    return plans


@router.get("/{plan_id}", response_model=BackupPlanResponse)
async def get_backup_plan(plan_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """获取单个备份计划信息"""
    plan = db.query(BackupPlan).filter(BackupPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="备份计划不存在")
    return plan


@router.post("/", response_model=BackupPlanResponse)
async def create_backup_plan(
    plan: BackupPlanCreate, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """创建备份计划"""
    # 验证数据库是否存在
    database = db.query(Database).filter(Database.id == plan.database_id).first()
    if not database:
        raise HTTPException(status_code=400, detail="数据库不存在")
    
    # 检查名称是否重复
    existing = db.query(BackupPlan).filter(BackupPlan.name == plan.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="计划名称已存在")
    
    # 验证备份类型
    if plan.backup_type not in ["full", "incremental"]:
        raise HTTPException(status_code=400, detail="备份类型必须是 full 或 incremental")
    
    # 验证调度配置
    if not plan.schedule_interval:
        raise HTTPException(status_code=400, detail="必须配置备份间隔（分钟）")
    
    db_plan = BackupPlan(
        name=plan.name,
        database_id=plan.database_id,
        backup_type=BackupType(plan.backup_type),
        schedule_interval=plan.schedule_interval,
        retention_count=plan.retention_count,
        is_active=plan.is_active
    )
    db.add(db_plan)
    db.commit()
    db.refresh(db_plan)
    
    log_operation(db, current_user, "创建备份计划", "backup_plan", db_plan.id, db_plan.name)
    
    return db_plan


@router.put("/{plan_id}", response_model=BackupPlanResponse)
async def update_backup_plan(
    plan_id: int,
    plan: BackupPlanUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """更新备份计划"""
    db_plan = db.query(BackupPlan).filter(BackupPlan.id == plan_id).first()
    if not db_plan:
        raise HTTPException(status_code=404, detail="备份计划不存在")
    
    old_is_active = db_plan.is_active
    
    # 更新字段
    if plan.name is not None:
        # 检查名称是否重复
        existing = db.query(BackupPlan).filter(
            BackupPlan.name == plan.name,
            BackupPlan.id != plan_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="计划名称已存在")
        db_plan.name = plan.name
    
    if plan.database_id is not None:
        database = db.query(Database).filter(Database.id == plan.database_id).first()
        if not database:
            raise HTTPException(status_code=400, detail="数据库不存在")
        db_plan.database_id = plan.database_id
    
    if plan.backup_type is not None:
        if plan.backup_type not in ["full", "incremental"]:
            raise HTTPException(status_code=400, detail="备份类型必须是 full 或 incremental")
        db_plan.backup_type = BackupType(plan.backup_type)
    
    if plan.schedule_interval is not None:
        db_plan.schedule_interval = plan.schedule_interval
    
    if plan.retention_count is not None:
        db_plan.retention_count = plan.retention_count
    
    if plan.is_active is not None:
        db_plan.is_active = plan.is_active
    
    db.commit()
    db.refresh(db_plan)
    
    # 当 is_active 状态变更时，启动或停止备份计划
    new_is_active = db_plan.is_active
    if old_is_active != new_is_active:
        try:
            if new_is_active:
                await backup_service.start_plan(plan_id)
                log_operation(db, current_user, "启用备份计划", "backup_plan", db_plan.id, db_plan.name)
            else:
                await backup_service.stop_plan(plan_id)
                log_operation(db, current_user, "禁用备份计划", "backup_plan", db_plan.id, db_plan.name)
        except Exception as e:
            log_operation(db, current_user, "启停备份计划失败", "backup_plan", db_plan.id, db_plan.name, str(e))
            raise HTTPException(status_code=500, detail=f"启停备份计划失败: {str(e)}")
    else:
        log_operation(db, current_user, "更新备份计划", "backup_plan", db_plan.id, db_plan.name)
    
    return db_plan


@router.delete("/{plan_id}")
async def delete_backup_plan(
    plan_id: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """删除备份计划"""
    from ..models.database import BackupHistory
    
    db_plan = db.query(BackupPlan).filter(BackupPlan.id == plan_id).first()
    if not db_plan:
        raise HTTPException(status_code=404, detail="备份计划不存在")
    
    plan_name = db_plan.name
    
    # 先删除关联的备份历史记录
    db.query(BackupHistory).filter(BackupHistory.backup_plan_id == plan_id).delete()
    
    db.delete(db_plan)
    db.commit()
    
    log_operation(db, current_user, "删除备份计划", "backup_plan", plan_id, plan_name)
    
    return {"message": "删除成功"}


@router.post("/{plan_id}/execute")
async def execute_backup_plan(
    plan_id: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """立即执行备份计划"""
    plan = db.query(BackupPlan).filter(BackupPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="备份计划不存在")
    
    try:
        await backup_service.execute_backup(plan_id)
        log_operation(db, current_user, "执行备份", "backup_plan", plan_id, plan.name, "备份任务已提交")
        return {"message": "备份任务已提交", "plan_id": plan_id}
    except Exception as e:
        log_operation(db, current_user, "执行备份失败", "backup_plan", plan_id, plan.name, str(e))
        raise HTTPException(status_code=500, detail=f"执行备份失败: {str(e)}")