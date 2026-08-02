"""
数据库管理API
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from ..core.database import get_db
from ..core.crypto import encrypt, decrypt
from ..models.database import Database, DatabaseType, User
from .auth import get_current_user, log_operation

router = APIRouter()


class DatabaseCreate(BaseModel):
    """创建数据库请求模型"""
    name: str
    host: str
    port: int = 3306
    username: str
    password: str
    database_name: str
    db_type: str = "mysql"
    is_active: bool = True


class DatabaseUpdate(BaseModel):
    """更新数据库请求模型"""
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    database_name: Optional[str] = None
    db_type: Optional[str] = None
    is_active: Optional[bool] = None


class DatabaseResponse(BaseModel):
    """数据库响应模型"""
    id: int
    name: str
    host: str
    port: int = 3306
    username: str
    password_set: bool = False  # 是否已设置密码（不返回明文）
    database_name: str
    db_type: str = "mysql"
    is_active: bool = True
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TestConnectionRequest(BaseModel):
    """测试连接请求模型"""
    host: str
    port: int = 3306
    username: str
    password: str
    database_name: str


@router.get("/", response_model=List[DatabaseResponse])
async def list_databases(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取数据库列表"""
    databases = db.query(Database).offset(skip).limit(limit).all()
    results = []
    for d in databases:
        results.append(DatabaseResponse(
            id=d.id, name=d.name, host=d.host, port=d.port,
            username=d.username, password_set=bool(d.password),
            database_name=d.database_name, db_type=d.db_type.value if d.db_type else "mysql",
            is_active=d.is_active, created_at=d.created_at, updated_at=d.updated_at
        ))
    return results


@router.get("/{database_id}", response_model=DatabaseResponse)
async def get_database(database_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """获取单个数据库信息"""
    d = db.query(Database).filter(Database.id == database_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="数据库不存在")
    return DatabaseResponse(
        id=d.id, name=d.name, host=d.host, port=d.port,
        username=d.username, password_set=bool(d.password),
        database_name=d.database_name, db_type=d.db_type.value if d.db_type else "mysql",
        is_active=d.is_active, created_at=d.created_at, updated_at=d.updated_at
    )


@router.post("/", response_model=DatabaseResponse)
async def create_database(
    database: DatabaseCreate, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """创建数据库配置"""
    # 检查名称是否重复
    existing = db.query(Database).filter(Database.name == database.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="数据库名称已存在")
    
    db_database = Database(
        name=database.name,
        host=database.host,
        port=database.port,
        username=database.username,
        password=encrypt(database.password),
        database_name=database.database_name,
        db_type=DatabaseType(database.db_type),
        is_active=database.is_active
    )
    db.add(db_database)
    db.commit()
    db.refresh(db_database)
    
    log_operation(db, current_user, "创建数据库", "database", db_database.id, db_database.name)
    
    return db_database


@router.put("/{database_id}", response_model=DatabaseResponse)
async def update_database(
    database_id: int,
    database: DatabaseUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """更新数据库配置"""
    db_database = db.query(Database).filter(Database.id == database_id).first()
    if not db_database:
        raise HTTPException(status_code=404, detail="数据库不存在")
    
    # 更新字段
    if database.name is not None:
        # 检查名称是否重复
        existing = db.query(Database).filter(
            Database.name == database.name,
            Database.id != database_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="数据库名称已存在")
        db_database.name = database.name
    
    if database.host is not None:
        db_database.host = database.host
    if database.port is not None:
        db_database.port = database.port
    if database.username is not None:
        db_database.username = database.username
    if database.password is not None:
        db_database.password = encrypt(database.password)
    if database.database_name is not None:
        db_database.database_name = database.database_name
    if database.db_type is not None:
        db_database.db_type = DatabaseType(database.db_type)
    if database.is_active is not None:
        db_database.is_active = database.is_active
    
    db.commit()
    db.refresh(db_database)
    
    log_operation(db, current_user, "更新数据库", "database", db_database.id, db_database.name)
    
    return db_database


@router.delete("/{database_id}")
async def delete_database(
    database_id: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """删除数据库配置"""
    db_database = db.query(Database).filter(Database.id == database_id).first()
    if not db_database:
        raise HTTPException(status_code=404, detail="数据库不存在")
    
    # 检查是否有关联的同步任务或备份计划
    from ..models.database import SyncTask, BackupPlan
    sync_tasks = db.query(SyncTask).filter(
        (SyncTask.source_db_id == database_id) | (SyncTask.target_db_id == database_id)
    ).count()
    backup_plans = db.query(BackupPlan).filter(BackupPlan.database_id == database_id).count()
    
    if sync_tasks > 0 or backup_plans > 0:
        raise HTTPException(
            status_code=400,
            detail="该数据库存在关联的同步任务或备份计划，无法删除"
        )
    
    db_name = db_database.name
    db.delete(db_database)
    db.commit()
    
    log_operation(db, current_user, "删除数据库", "database", database_id, db_name)
    
    return {"message": "删除成功"}


@router.post("/test-connection")
async def test_connection(request: TestConnectionRequest):
    """测试数据库连接"""
    try:
        import mysql.connector
        connection = mysql.connector.connect(
            host=request.host,
            port=request.port,
            user=request.username,
            password=request.password,
            database=request.database_name,
            connect_timeout=5,
            ssl_disabled=True  # 禁用 SSL（避免自签名证书问题）
        )
        connection.close()
        return {"success": True, "message": "连接成功"}
    except Exception as e:
        return {"success": False, "message": f"连接失败: {str(e)}"}