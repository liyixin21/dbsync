"""
备份历史API
"""
import os
import subprocess
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime
from loguru import logger

from ..core.database import get_db
from ..core.config import now_beijing
from ..core.crypto import decrypt
from ..core.services import backup_service, sync_service
from ..services.backup_service import _find_mysql_tool
from ..models.database import BackupHistory, BackupStatus, BackupType, BackupPlan, Database, User, RunLog, SyncTask, SyncStatus
from .auth import get_current_user, log_operation

router = APIRouter()


class BackupHistoryResponse(BaseModel):
    """备份历史响应模型"""
    id: int
    backup_plan_id: int
    status: str
    backup_type: str
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration: Optional[int] = None
    error_message: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class BackupHistoryListResponse(BaseModel):
    """备份历史列表响应模型"""
    total: int
    items: List[BackupHistoryResponse]


@router.get("/statistics")
async def get_backup_statistics(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """获取备份统计信息"""
    from sqlalchemy import func
    
    # 总数统计
    total_count = db.query(func.count(BackupHistory.id)).scalar()
    
    # 状态统计
    status_stats = db.query(
        BackupHistory.status,
        func.count(BackupHistory.id)
    ).group_by(BackupHistory.status).all()
    
    # 类型统计
    type_stats = db.query(
        BackupHistory.backup_type,
        func.count(BackupHistory.id)
    ).group_by(BackupHistory.backup_type).all()
    
    # 最近7天备份数量
    from datetime import timedelta
    seven_days_ago = now_beijing() - timedelta(days=7)
    recent_count = db.query(func.count(BackupHistory.id)).filter(
        BackupHistory.created_at >= seven_days_ago
    ).scalar()
    
    # 总备份大小
    total_size = db.query(func.sum(BackupHistory.file_size)).scalar() or 0
    
    return {
        "total_count": total_count,
        "status_stats": {status.value: count for status, count in status_stats},
        "type_stats": {backup_type.value: count for backup_type, count in type_stats},
        "recent_count": recent_count,
        "total_size": total_size
    }


@router.get("/", response_model=BackupHistoryListResponse)
async def list_backup_history(
    plan_id: Optional[int] = None,
    status: Optional[str] = None,
    backup_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取备份历史列表"""
    query = db.query(BackupHistory)
    
    # 应用过滤条件
    if plan_id:
        query = query.filter(BackupHistory.backup_plan_id == plan_id)
    if status:
        query = query.filter(BackupHistory.status == BackupStatus(status))
    if backup_type:
        query = query.filter(BackupHistory.backup_type == BackupType(backup_type))
    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            query = query.filter(BackupHistory.created_at >= start_dt)
        except ValueError:
            pass
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            query = query.filter(BackupHistory.created_at <= end_dt)
        except ValueError:
            pass
    
    # 获取总数
    total = query.count()
    
    # 分页查询
    history = query.order_by(BackupHistory.created_at.desc()).offset(skip).limit(limit).all()
    
    return BackupHistoryListResponse(total=total, items=history)


@router.get("/{history_id}", response_model=BackupHistoryResponse)
async def get_backup_history(history_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """获取单个备份历史记录"""
    history = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
    if not history:
        raise HTTPException(status_code=404, detail="备份历史记录不存在")
    return history


@router.delete("/clear")
async def clear_backup_history(
    delete_files: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """清空所有备份历史记录"""
    histories = db.query(BackupHistory).all()
    count = len(histories)
    
    if delete_files:
        import os
        for history in histories:
            if history.file_path:
                try:
                    if os.path.exists(history.file_path):
                        os.remove(history.file_path)
                except Exception:
                    pass
    
    db.query(BackupHistory).delete()
    db.commit()
    # 记录操作日志
    log_operation(db, current_user, "清空备份历史", "backup_history", None, None, f"已清空 {count} 条备份历史记录")
    return {"message": f"已清空 {count} 条备份历史记录"}


@router.delete("/{history_id}")
async def delete_backup_history(history_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """删除备份历史记录"""
    history = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
    if not history:
        raise HTTPException(status_code=404, detail="备份历史记录不存在")
    
    # 删除备份文件
    if history.file_path:
        import os
        try:
            if os.path.exists(history.file_path):
                os.remove(history.file_path)
        except Exception:
            pass
    
    db.delete(history)
    db.commit()
    # 记录操作日志
    log_operation(db, current_user, "删除备份记录", "backup_history", history_id, f"备份#{history_id}")
    return {"message": "删除成功"}


@router.delete("/batch")
async def batch_delete_backup_history(
    ids: List[int],
    delete_files: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """批量删除备份历史记录"""
    histories = db.query(BackupHistory).filter(BackupHistory.id.in_(ids)).all()
    if not histories:
        raise HTTPException(status_code=404, detail="未找到指定的备份历史记录")
    
    # 删除备份文件
    if delete_files:
        import os
        for history in histories:
            if history.file_path:
                try:
                    if os.path.exists(history.file_path):
                        os.remove(history.file_path)
                except Exception:
                    pass
    
    # 删除数据库记录
    for history in histories:
        db.delete(history)

    db.commit()
    # 记录操作日志
    log_operation(db, current_user, "批量删除备份记录", "backup_history", None, None, f"成功删除 {len(histories)} 条记录")
    return {"message": f"成功删除 {len(histories)} 条记录"}


@router.get("/{history_id}/download")
async def download_backup_file(history_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """下载备份文件"""
    history = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
    if not history:
        raise HTTPException(status_code=404, detail="备份历史记录不存在")

    if not history.file_path or not os.path.exists(history.file_path):
        raise HTTPException(status_code=404, detail="备份文件不存在或已被删除")

    filename = os.path.basename(history.file_path)
    return FileResponse(
        path=history.file_path,
        filename=filename,
        media_type="application/octet-stream"
    )


class RestoreRequest(BaseModel):
    """恢复请求模型"""
    target_database_id: Optional[int] = None  # 不指定则恢复到原数据库


@router.post("/{history_id}/restore")
async def restore_backup(
    history_id: int,
    request: RestoreRequest = RestoreRequest(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """从备份文件恢复数据库"""
    logger.info(f"[Restore] 收到恢复请求: history_id={history_id}, target_db_id={request.target_database_id}, user={current_user.username}")
    history = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
    if not history:
        logger.warning(f"[Restore] 备份记录不存在: history_id={history_id}")
        raise HTTPException(status_code=404, detail="备份历史记录不存在")

    if not history.file_path or not os.path.exists(history.file_path):
        raise HTTPException(status_code=404, detail="备份文件不存在或已被删除")

    # 获取备份计划和关联数据库
    plan = db.query(BackupPlan).filter(BackupPlan.id == history.backup_plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="关联的备份计划不存在")

    # 确定目标数据库
    if request.target_database_id:
        target_db = db.query(Database).filter(Database.id == request.target_database_id).first()
    else:
        target_db = db.query(Database).filter(Database.id == plan.database_id).first()

    if not target_db:
        raise HTTPException(status_code=404, detail="目标数据库不存在")

    # 查找与目标数据库相关的同步任务（作为源或目标）
    affected_sync_tasks = db.query(SyncTask).filter(
        (SyncTask.source_db_id == target_db.id) | (SyncTask.target_db_id == target_db.id),
        SyncTask.status == SyncStatus.RUNNING
    ).all()
    affected_task_ids = [t.id for t in affected_sync_tasks]

    # 记录运行日志：开始恢复
    restore_run_log = RunLog(
        task_type="restore",
        task_id=history_id,
        task_name=f"备份#{history_id}",
        level="INFO",
        message=f"开始恢复数据库到 {target_db.name} ({target_db.host}:{target_db.port}/{target_db.database_name})",
        detail=f"备份文件: {history.file_path}, 操作用户: {current_user.username}"
    )
    db.add(restore_run_log)
    db.commit()

    # 暂停相关的同步任务，避免 DDL 冲突
    paused_tasks = []
    for task_id in affected_task_ids:
        try:
            sync = sync_service.sync_tasks.get(task_id)
            if sync and sync.running:
                sync.stop()
                paused_tasks.append(task_id)
                logger.info(f"[Restore] 已暂停同步任务 {task_id}")
        except Exception as e:
            logger.warning(f"[Restore] 暂停同步任务 {task_id} 失败: {e}")

    try:
        # 构建 mysql 恢复命令（使用解密后的密码和正确的工具路径）
        mysql_path = _find_mysql_tool('mysql')
        logger.info(f"[Restore] MySQL工具路径: {mysql_path}")
        logger.info(f"[Restore] 目标数据库: {target_db.host}:{target_db.port}/{target_db.database_name}")
        logger.info(f"[Restore] 备份文件: {history.file_path}")
        
        # 通过环境变量传密码，避免在进程列表中暴露
        import os as _os
        restore_env = {**_os.environ, 'MYSQL_PWD': decrypt(target_db.password)}
        
        cmd = [
            mysql_path,
            f'--host={target_db.host}',
            f'--port={target_db.port}',
            f'--user={target_db.username}',
            target_db.database_name
        ]

        with open(history.file_path, 'r') as f:
            result = subprocess.run(
                cmd, stdin=f, stderr=subprocess.PIPE,
                text=True, timeout=3600, env=restore_env
            )

        if result.returncode == 0:
            logger.info(f"[Restore] 数据库恢复成功: {target_db.host}:{target_db.port}/{target_db.database_name}")
            # 记录操作日志
            log_operation(
                db, current_user, "恢复数据库", "backup_history", history_id,
                f"备份#{history_id}",
                f"恢复到 {target_db.name} ({target_db.host}:{target_db.port}/{target_db.database_name})"
            )
            # 记录运行日志：恢复成功
            success_log = RunLog(
                task_type="restore",
                task_id=history_id,
                task_name=f"备份#{history_id}",
                level="INFO",
                message=f"数据库恢复成功: {target_db.name} ({target_db.host}:{target_db.port}/{target_db.database_name})",
                detail=f"操作用户: {current_user.username}"
            )
            db.add(success_log)
            db.commit()
            return {"message": "数据库恢复成功", "target_database": target_db.name}
        else:
            logger.error(f"[Restore] 数据库恢复失败: {result.stderr}")
            # 记录失败日志
            log_operation(
                db, current_user, "恢复数据库失败", "backup_history", history_id,
                f"备份#{history_id}",
                f"恢复失败: {result.stderr[:200]}"
            )
            # 记录运行日志：恢复失败
            fail_log = RunLog(
                task_type="restore",
                task_id=history_id,
                task_name=f"备份#{history_id}",
                level="ERROR",
                message=f"数据库恢复失败: {result.stderr[:200]}",
                detail=f"操作用户: {current_user.username}, 返回码: {result.returncode}"
            )
            db.add(fail_log)
            db.commit()
            raise HTTPException(status_code=500, detail=f"恢复失败: {result.stderr}")

    except subprocess.TimeoutExpired:
        logger.error(f"[Restore] 恢复操作超时: {history_id}")
        log_operation(
            db, current_user, "恢复数据库失败", "backup_history", history_id,
            f"备份#{history_id}", "恢复操作超时"
        )
        timeout_log = RunLog(
            task_type="restore",
            task_id=history_id,
            task_name=f"备份#{history_id}",
            level="ERROR",
            message="恢复操作超时",
            detail=f"操作用户: {current_user.username}"
        )
        db.add(timeout_log)
        db.commit()
        raise HTTPException(status_code=500, detail="恢复操作超时")
    except Exception as e:
        logger.error(f"[Restore] 恢复异常: {e}")
        log_operation(
            db, current_user, "恢复数据库失败", "backup_history", history_id,
            f"备份#{history_id}", f"恢复异常: {str(e)[:200]}"
        )
        error_log = RunLog(
            task_type="restore",
            task_id=history_id,
            task_name=f"备份#{history_id}",
            level="ERROR",
            message=f"恢复异常: {str(e)[:200]}",
            detail=f"操作用户: {current_user.username}"
        )
        db.add(error_log)
        db.commit()
        raise HTTPException(status_code=500, detail=f"恢复异常: {str(e)}")
    finally:
        # 恢复之前暂停的同步任务
        for task_id in paused_tasks:
            try:
                # 清理旧的 sync 实例并重新启动
                if task_id in sync_service.sync_tasks:
                    del sync_service.sync_tasks[task_id]
                await sync_service.start_task(task_id)
                logger.info(f"[Restore] 已恢复同步任务 {task_id}")
            except Exception as e:
                logger.warning(f"[Restore] 恢复同步任务 {task_id} 失败: {e}")