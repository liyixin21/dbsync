"""
同步任务API
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from ..core.database import get_db
from ..core.services import sync_service
from ..models.database import SyncTask, SyncStatus, Database, User
from .auth import get_current_user, log_operation

router = APIRouter()


class SyncTaskCreate(BaseModel):
    """创建同步任务请求模型"""
    name: str
    source_db_id: int
    target_db_id: int


class SyncTaskUpdate(BaseModel):
    """更新同步任务请求模型"""
    name: Optional[str] = None
    source_db_id: Optional[int] = None
    target_db_id: Optional[int] = None
    status: Optional[str] = None


class SyncTaskResponse(BaseModel):
    """同步任务响应模型"""
    id: int
    name: str
    source_db_id: int
    target_db_id: int
    status: str
    last_sync_time: Optional[datetime] = None
    sync_delay: Optional[int] = 0
    error_message: Optional[str] = None
    binlog_position: Optional[str] = None
    binlog_file: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class SyncTaskStatus(BaseModel):
    """同步任务状态响应模型"""
    task_id: int
    task_name: str
    status: str
    last_sync_time: Optional[datetime]
    sync_delay: int
    error_message: Optional[str]
    source_db: str
    target_db: str


@router.get("/", response_model=List[SyncTaskResponse])
async def list_sync_tasks(
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取同步任务列表"""
    query = db.query(SyncTask)
    if status:
        query = query.filter(SyncTask.status == SyncStatus(status))
    tasks = query.offset(skip).limit(limit).all()
    return tasks


@router.get("/{task_id}", response_model=SyncTaskResponse)
async def get_sync_task(task_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """获取单个同步任务信息"""
    task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="同步任务不存在")
    return task


@router.post("/", response_model=SyncTaskResponse)
async def create_sync_task(
    task: SyncTaskCreate, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """创建同步任务"""
    # 验证源数据库和目标数据库是否存在
    source_db = db.query(Database).filter(Database.id == task.source_db_id).first()
    target_db = db.query(Database).filter(Database.id == task.target_db_id).first()
    
    if not source_db:
        raise HTTPException(status_code=400, detail="源数据库不存在")
    if not target_db:
        raise HTTPException(status_code=400, detail="目标数据库不存在")
    if task.source_db_id == task.target_db_id:
        raise HTTPException(status_code=400, detail="源数据库和目标数据库不能相同")
    
    # 检查名称是否重复
    existing = db.query(SyncTask).filter(SyncTask.name == task.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="任务名称已存在")
    
    db_task = SyncTask(
        name=task.name,
        source_db_id=task.source_db_id,
        target_db_id=task.target_db_id,
        status=SyncStatus.PENDING
    )
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    
    log_operation(db, current_user, "创建同步任务", "sync_task", db_task.id, db_task.name)
    
    return db_task


@router.put("/{task_id}", response_model=SyncTaskResponse)
async def update_sync_task(
    task_id: int,
    task: SyncTaskUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """更新同步任务"""
    db_task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
    if not db_task:
        raise HTTPException(status_code=404, detail="同步任务不存在")
    
    # 更新字段
    if task.name is not None:
        # 检查名称是否重复
        existing = db.query(SyncTask).filter(
            SyncTask.name == task.name,
            SyncTask.id != task_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="任务名称已存在")
        db_task.name = task.name
    
    if task.source_db_id is not None:
        source_db = db.query(Database).filter(Database.id == task.source_db_id).first()
        if not source_db:
            raise HTTPException(status_code=400, detail="源数据库不存在")
        db_task.source_db_id = task.source_db_id
    
    if task.target_db_id is not None:
        target_db = db.query(Database).filter(Database.id == task.target_db_id).first()
        if not target_db:
            raise HTTPException(status_code=400, detail="目标数据库不存在")
        db_task.target_db_id = task.target_db_id
    
    if task.status is not None:
        db_task.status = SyncStatus(task.status)
    
    db.commit()
    db.refresh(db_task)
    return db_task


@router.delete("/{task_id}")
async def delete_sync_task(
    task_id: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """删除同步任务"""
    db_task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
    if not db_task:
        raise HTTPException(status_code=404, detail="同步任务不存在")
    
    # 如果任务正在运行，先停止它
    if db_task.status == SyncStatus.RUNNING:
        await sync_service.stop_task(task_id)
    
    task_name = db_task.name
    db.delete(db_task)
    db.commit()
    
    log_operation(db, current_user, "删除同步任务", "sync_task", task_id, task_name)
    
    return {"message": "删除成功"}


@router.post("/{task_id}/start")
async def start_sync_task(
    task_id: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """启动同步任务"""
    task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="同步任务不存在")
    
    if task.status == SyncStatus.RUNNING:
        raise HTTPException(status_code=400, detail="任务已在运行中")
    
    try:
        await sync_service.start_task(task_id)
        db.refresh(task)
        log_operation(db, current_user, "启动同步任务", "sync_task", task_id, task.name, "任务启动成功")
        return {"message": "任务启动成功", "task_id": task_id}
    except Exception as e:
        log_operation(db, current_user, "启动同步任务失败", "sync_task", task_id, task.name, str(e))
        raise HTTPException(status_code=500, detail=f"启动任务失败: {str(e)}")


@router.post("/{task_id}/stop")
async def stop_sync_task(
    task_id: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """停止同步任务"""
    task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="同步任务不存在")
    
    if task.status != SyncStatus.RUNNING:
        raise HTTPException(status_code=400, detail="任务未在运行中")
    
    try:
        await sync_service.stop_task(task_id)
        db.refresh(task)
        log_operation(db, current_user, "停止同步任务", "sync_task", task_id, task.name, "任务停止成功")
        return {"message": "任务停止成功", "task_id": task_id}
    except Exception as e:
        log_operation(db, current_user, "停止同步任务失败", "sync_task", task_id, task.name, str(e))
        raise HTTPException(status_code=500, detail=f"停止任务失败: {str(e)}")


@router.post("/{task_id}/full-copy")
async def full_copy_database(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """一键复制：将源数据库所有数据完整复制到目标数据库"""
    import subprocess
    from ..core.crypto import decrypt
    from ..services.backup_service import _find_mysql_tool
    from ..models.database import RunLog
    from loguru import logger
    
    task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="同步任务不存在")
    
    source_db = db.query(Database).filter(Database.id == task.source_db_id).first()
    target_db = db.query(Database).filter(Database.id == task.target_db_id).first()
    
    if not source_db or not target_db:
        raise HTTPException(status_code=404, detail="源数据库或目标数据库不存在")
    
    # 记录运行日志
    run_log = RunLog(
        task_type="sync",
        task_id=task_id,
        task_name=task.name,
        level="INFO",
        message=f"开始全量复制: {source_db.name} → {target_db.name}",
        detail=f"源: {source_db.host}:{source_db.port}/{source_db.database_name}, 目标: {target_db.host}:{target_db.port}/{target_db.database_name}"
    )
    db.add(run_log)
    db.commit()
    
    try:
        mysqldump_path = _find_mysql_tool('mysqldump')
        mysql_path = _find_mysql_tool('mysql')
        
        source_password = decrypt(source_db.password)
        target_password = decrypt(target_db.password)
        
        # 通过环境变量传密码，避免在进程列表中暴露
        import os as _os
        dump_env = {**_os.environ, 'MYSQL_PWD': source_password}
        import_env = {**_os.environ, 'MYSQL_PWD': target_password}
        
        # Step 1: mysqldump 导出源数据库（使用更完整的参数确保数据完整性）
        dump_cmd = [
            mysqldump_path,
            f'--host={source_db.host}',
            f'--port={source_db.port}',
            f'--user={source_db.username}',
            '--single-transaction',
            '--routines',
            '--triggers',
            '--events',
            '--hex-blob',                       # 以十六进制格式处理 BLOB/BINARY 列
            '--default-character-set=utf8mb4',  # 指定字符集
            '--complete-insert',                # 生成完整 INSERT 语句（包含列名）
            '--skip-lock-tables',               # 不锁定表
            '--ssl=0',                           # 禁用 SSL（兼容新旧版本）
            source_db.database_name
        ]
        
        # Step 2: mysql 导入到目标数据库
        import_cmd = [
            mysql_path,
            f'--host={target_db.host}',
            f'--port={target_db.port}',
            f'--user={target_db.username}',
            '--default-character-set=utf8mb4',  # 指定字符集
            '--force',                          # 遇到错误继续执行
            '--ssl=0',                           # 禁用 SSL（兼容新旧版本）
            '--init-command=SET FOREIGN_KEY_CHECKS=0, UNIQUE_CHECKS=0',  # 禁用检查提高导入速度和可靠性
            target_db.database_name
        ]
        
        logger.info(f"[FullCopy] 开始全量复制: {source_db.name} → {target_db.name}")
        
        # 使用临时文件避免管道死锁
        import tempfile
        import os
        
        tmp_fd, tmp_path = tempfile.mkstemp(suffix='.sql')
        os.close(tmp_fd)
        
        try:
            # Step 1: mysqldump 导出直接写入临时文件（避免内存溢出和二进制数据损坏）
            with open(tmp_path, 'wb') as f:
                dump_result = subprocess.run(
                    dump_cmd, stdout=f, stderr=subprocess.PIPE, timeout=3600, env=dump_env
                )
            
            if dump_result.returncode != 0:
                error_msg = dump_result.stderr.decode('utf-8', errors='replace').strip()
                raise Exception(f"导出失败: {error_msg}")
            
            dump_size = os.path.getsize(tmp_path)
            logger.info(f"[FullCopy] 导出完成，文件大小: {dump_size / 1024 / 1024:.2f} MB，开始导入...")
            
            # Step 2: mysql 从临时文件导入
            # 先禁用外键检查，导入完成后恢复（解决表顺序依赖问题）
            with open(tmp_path, 'rb') as f:
                import_result = subprocess.run(
                    import_cmd, stdin=f, stderr=subprocess.PIPE, timeout=3600, env=import_env
                )
            
            if import_result.returncode != 0:
                error_msg = import_result.stderr.decode('utf-8', errors='replace').strip()
                raise Exception(f"导入失败: {error_msg}")
        finally:
            # 清理临时文件
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        
        logger.info(f"[FullCopy] 全量复制成功: {source_db.name} → {target_db.name}")
        
        # 记录成功日志
        success_log = RunLog(
            task_type="sync",
            task_id=task_id,
            task_name=task.name,
            level="INFO",
            message=f"全量复制完成: {source_db.name} → {target_db.name}",
            detail=f"操作用户: {current_user.username}"
        )
        db.add(success_log)
        
        log_operation(db, current_user, "全量复制", "sync_task", task_id, task.name, 
                     f"从 {source_db.name} 复制到 {target_db.name}")
        
        db.commit()
        return {"message": "全量复制完成", "source": source_db.name, "target": target_db.name}
        
    except Exception as e:
        logger.error(f"[FullCopy] 全量复制失败: {e}")
        
        # 记录失败日志
        fail_log = RunLog(
            task_type="sync",
            task_id=task_id,
            task_name=task.name,
            level="ERROR",
            message=f"全量复制失败: {str(e)[:200]}",
            detail=f"操作用户: {current_user.username}"
        )
        db.add(fail_log)
        
        log_operation(db, current_user, "全量复制失败", "sync_task", task_id, task.name, str(e))
        
        db.commit()
        raise HTTPException(status_code=500, detail=f"全量复制失败: {str(e)}")


@router.get("/{task_id}/status", response_model=SyncTaskStatus)
async def get_sync_task_status(task_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """获取同步任务状态"""
    task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="同步任务不存在")
    
    source_db = db.query(Database).filter(Database.id == task.source_db_id).first()
    target_db = db.query(Database).filter(Database.id == task.target_db_id).first()
    
    return SyncTaskStatus(
        task_id=task.id,
        task_name=task.name,
        status=task.status.value,
        last_sync_time=task.last_sync_time,
        sync_delay=task.sync_delay,
        error_message=task.error_message,
        source_db=source_db.name if source_db else "未知",
        target_db=target_db.name if target_db else "未知"
    )