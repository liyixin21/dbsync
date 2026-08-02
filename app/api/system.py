"""
系统管理API
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
from datetime import datetime

from ..core.database import get_db
from ..core.config import settings
from ..models.database import SystemConfig, User
from .auth import get_current_user

router = APIRouter()


class SystemConfigResponse(BaseModel):
    """系统配置响应模型"""
    key: str
    value: Optional[str]
    description: Optional[str]


class SystemConfigUpdate(BaseModel):
    """系统配置更新请求模型"""
    value: str
    description: Optional[str] = None


class SystemStatus(BaseModel):
    """系统状态响应模型"""
    app_name: str
    app_version: str
    uptime: float
    sync_tasks_count: int
    backup_plans_count: int
    last_backup_time: Optional[datetime]
    disk_usage: Dict[str, Any]


@router.get("/status", response_model=SystemStatus)
async def get_system_status(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """获取系统状态"""
    import psutil
    import os
    from ..models.database import SyncTask, BackupPlan, BackupHistory
    
    # 获取同步任务数量
    sync_tasks_count = db.query(SyncTask).count()
    
    # 获取备份计划数量
    backup_plans_count = db.query(BackupPlan).count()
    
    # 获取最后备份时间
    last_backup = db.query(BackupHistory).order_by(
        BackupHistory.created_at.desc()
    ).first()
    last_backup_time = last_backup.created_at if last_backup else None
    
    # 获取磁盘使用情况
    disk_usage = {}
    try:
        backup_path = settings.BACKUP_DIR
        if os.path.exists(backup_path):
            usage = psutil.disk_usage(backup_path)
            disk_usage = {
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent
            }
    except Exception:
        pass
    
    # 获取系统运行时间
    import time
    uptime = time.time() - psutil.boot_time()
    
    return SystemStatus(
        app_name=settings.APP_NAME,
        app_version=settings.APP_VERSION,
        uptime=uptime,
        sync_tasks_count=sync_tasks_count,
        backup_plans_count=backup_plans_count,
        last_backup_time=last_backup_time,
        disk_usage=disk_usage
    )


@router.get("/configs", response_model=List[SystemConfigResponse])
async def list_system_configs(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """获取系统配置列表"""
    configs = db.query(SystemConfig).all()
    return configs


@router.get("/configs/{key}", response_model=SystemConfigResponse)
async def get_system_config(key: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """获取单个系统配置"""
    config = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if not config:
        raise HTTPException(status_code=404, detail="配置不存在")
    return config


@router.put("/configs/{key}", response_model=SystemConfigResponse)
async def update_system_config(
    key: str,
    config: SystemConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """更新系统配置"""
    db_config = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    
    if db_config:
        db_config.value = config.value
        if config.description is not None:
            db_config.description = config.description
    else:
        # 创建新配置
        db_config = SystemConfig(
            key=key,
            value=config.value,
            description=config.description
        )
        db.add(db_config)
    
    db.commit()
    db.refresh(db_config)
    return db_config


@router.get("/theme")
async def get_theme_config(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """获取主题配置"""
    # 默认主题配置
    default_theme = {
        "primary_color": "#6750A4",
        "theme_scheme": "TonalSpot",
        "contrast_level": "0",
        "dark_mode": False
    }
    
    # 从数据库获取配置
    configs = db.query(SystemConfig).filter(
        SystemConfig.key.in_(["primary_color", "theme_scheme", "contrast_level", "dark_mode"])
    ).all()
    
    # 更新配置
    for config in configs:
        if config.key == "primary_color":
            default_theme["primary_color"] = config.value
        elif config.key == "theme_scheme":
            default_theme["theme_scheme"] = config.value
        elif config.key == "contrast_level":
            default_theme["contrast_level"] = config.value
        elif config.key == "dark_mode":
            default_theme["dark_mode"] = config.value.lower() == "true"
    
    return default_theme


@router.put("/theme")
async def update_theme_config(theme: Dict[str, Any], db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """更新主题配置"""
    # 更新各个配置项
    for key, value in theme.items():
        if key in ["primary_color", "theme_scheme", "contrast_level", "dark_mode"]:
            db_config = db.query(SystemConfig).filter(SystemConfig.key == key).first()
            
            if db_config:
                db_config.value = str(value)
            else:
                db_config = SystemConfig(
                    key=key,
                    value=str(value),
                    description=f"主题配置 - {key}"
                )
                db.add(db_config)
    
    db.commit()
    return {"message": "主题配置更新成功"}


@router.get("/logs")
async def get_system_logs(
    lines: int = 100,
    level: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """获取系统日志"""
    import os
    from ..core.config import settings
    
    log_file = settings.LOG_FILE
    if not os.path.exists(log_file):
        return {"logs": []}
    
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
            
        # 获取最后N行
        last_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
        
        # 过滤日志级别
        if level:
            level = level.upper()
            last_lines = [line for line in last_lines if level in line]
        
        return {"logs": last_lines}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取日志失败: {str(e)}")