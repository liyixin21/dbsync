"""
数据库连接配置 API。

相比旧实现：test-connection 现在需要认证（旧版本任何人可探测内网 MySQL）。
"""
from datetime import datetime
from typing import List, Optional

import mysql.connector
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.crypto import CredentialDecryptError, decrypt, encrypt, is_encrypted
from ..core.database import get_db
from ..core.errors import (
    BadRequestError,
    CredentialError,
    DatabaseConnectionError,
    DatabaseInUseError,
    NameTakenError,
    NotFoundError,
)
from ..models.database import BackupPlan, Database, DatabaseType, SyncTask, User
from ..services.audit import log_operation
from ..services.mysql.conninfo import build_connect_kwargs, connect_plain_fallback
from .deps import client_ip, get_current_user

router = APIRouter()


# ============================================================ 模型

class DatabaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=3306, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)
    database_name: str = Field(min_length=1, max_length=100)
    db_type: str = "mysql"
    is_active: bool = True


class DatabaseUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    host: Optional[str] = Field(default=None, min_length=1, max_length=255)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = Field(default=None, min_length=1, max_length=100)
    password: Optional[str] = Field(default=None, max_length=500)
    database_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    db_type: Optional[str] = None
    is_active: Optional[bool] = None


class DatabaseResponse(BaseModel):
    id: int
    name: str
    host: str
    port: int
    username: str
    password_set: bool
    database_name: str
    db_type: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class TestConnectionRequest(BaseModel):
    """连接测试。支持两种模式：直接给明文，或引用已保存的数据库（密码留空）。"""

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=3306, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(default="", max_length=500)
    database_name: str = Field(min_length=1, max_length=100)
    database_id: Optional[int] = Field(
        default=None, description="编辑已有数据库且未改密码时传入，用已存密码测试"
    )


class TestConnectionResponse(BaseModel):
    success: bool
    message: str
    server_version: Optional[str] = None


class MessageResponse(BaseModel):
    message: str


# ============================================================ 辅助

def _to_response(db: Database) -> DatabaseResponse:
    return DatabaseResponse(
        id=db.id,
        name=db.name,
        host=db.host,
        port=db.port,
        username=db.username,
        password_set=bool(db.password),
        database_name=db.database_name,
        db_type=db.db_type.value if db.db_type else "mysql",
        is_active=db.is_active,
        created_at=db.created_at,
        updated_at=db.updated_at,
    )


def _assert_name_free(db: Session, name: str, exclude_id: Optional[int] = None) -> None:
    query = db.query(Database).filter(Database.name == name)
    if exclude_id is not None:
        query = query.filter(Database.id != exclude_id)
    if query.first() is not None:
        raise NameTakenError(f"数据库名称「{name}」已存在")


def _resolve_stored_password(payload: TestConnectionRequest, db: Session) -> str:
    """密码留空且给了 database_id 时，取用已保存的凭据。"""
    if payload.password:
        return payload.password
    if payload.database_id is None:
        raise BadRequestError("请输入密码")

    record = db.query(Database).filter(Database.id == payload.database_id).first()
    if record is None:
        raise NotFoundError("引用的数据库配置不存在")
    try:
        return decrypt(record.password)
    except CredentialDecryptError as exc:
        raise CredentialError(str(exc)) from exc


# ============================================================ 端点

@router.get("/", response_model=List[DatabaseResponse])
async def list_databases(
    skip: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[DatabaseResponse]:
    """数据库配置列表。"""
    rows = db.query(Database).order_by(Database.id).offset(max(0, skip)).limit(
        max(1, min(limit, 500))
    ).all()
    return [_to_response(row) for row in rows]


@router.post("/test-connection", response_model=TestConnectionResponse)
async def test_connection(
    payload: TestConnectionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TestConnectionResponse:
    """
    测试 MySQL 连接。

    注意：本端点在旧版本中缺失认证依赖，任何未登录用户都能用它探测内网数据库。
    """
    password = _resolve_stored_password(payload, db)

    try:
        conn = connect_plain_fallback(
            **build_connect_kwargs(
                host=payload.host,
                port=payload.port,
                user=payload.username,
                password=password,
                database=payload.database_name or None,
                connect_timeout=8,
            )
        )
    except mysql.connector.Error as exc:
        return TestConnectionResponse(success=False, message=f"连接失败: {_compact(exc)}")
    except Exception as exc:
        return TestConnectionResponse(success=False, message=f"连接失败: {exc}")

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT VERSION()")
        version = cursor.fetchone()
        cursor.close()
    except Exception:
        version = None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    server_version = version[0] if version else None
    suffix = f"，服务端版本 {server_version}" if server_version else ""
    return TestConnectionResponse(
        success=True, message=f"连接成功{suffix}", server_version=server_version
    )


def _compact(exc: Exception) -> str:
    text = str(exc)
    return text if len(text) <= 300 else text[:300] + "..."


@router.get("/{database_id}", response_model=DatabaseResponse)
async def get_database(
    database_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DatabaseResponse:
    record = db.query(Database).filter(Database.id == database_id).first()
    if record is None:
        raise NotFoundError("数据库不存在")
    return _to_response(record)


@router.post("/", response_model=DatabaseResponse)
async def create_database(
    payload: DatabaseCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DatabaseResponse:
    """新增数据库配置。密码加密存储。"""
    _assert_name_free(db, payload.name)

    try:
        db_type = DatabaseType(payload.db_type)
    except ValueError:
        raise BadRequestError(f"不支持的数据库类型: {payload.db_type}")

    record = Database(
        name=payload.name,
        host=payload.host,
        port=payload.port,
        username=payload.username,
        password=encrypt(payload.password),
        database_name=payload.database_name,
        db_type=db_type,
        is_active=payload.is_active,
    )
    db.add(record)
    db.flush()

    log_operation(
        db, current_user, "创建数据库", "database", record.id, record.name,
        f"{record.host}:{record.port}/{record.database_name}", ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(record)
    return _to_response(record)


@router.put("/{database_id}", response_model=DatabaseResponse)
async def update_database(
    database_id: int,
    payload: DatabaseUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DatabaseResponse:
    """更新数据库配置。密码留空表示不修改。"""
    record = db.query(Database).filter(Database.id == database_id).first()
    if record is None:
        raise NotFoundError("数据库不存在")

    if payload.name is not None:
        _assert_name_free(db, payload.name, exclude_id=database_id)
        record.name = payload.name
    if payload.host is not None:
        record.host = payload.host
    if payload.port is not None:
        record.port = payload.port
    if payload.username is not None:
        record.username = payload.username
    if payload.password:
        record.password = encrypt(payload.password)
    if payload.database_name is not None:
        record.database_name = payload.database_name
    if payload.db_type is not None:
        try:
            record.db_type = DatabaseType(payload.db_type)
        except ValueError:
            raise BadRequestError(f"不支持的数据库类型: {payload.db_type}")
    if payload.is_active is not None:
        record.is_active = payload.is_active

    log_operation(
        db, current_user, "更新数据库", "database", record.id, record.name,
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(record)
    return _to_response(record)


@router.delete("/{database_id}", response_model=MessageResponse)
async def delete_database(
    database_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """删除数据库配置。存在关联任务时拒绝。"""
    record = db.query(Database).filter(Database.id == database_id).first()
    if record is None:
        raise NotFoundError("数据库不存在")

    sync_count = (
        db.query(SyncTask)
        .filter(
            (SyncTask.source_db_id == database_id) | (SyncTask.target_db_id == database_id)
        )
        .count()
    )
    plan_count = db.query(BackupPlan).filter(BackupPlan.database_id == database_id).count()

    if sync_count or plan_count:
        parts = []
        if sync_count:
            parts.append(f"{sync_count} 个同步任务")
        if plan_count:
            parts.append(f"{plan_count} 个备份计划")
        raise DatabaseInUseError(
            f"该数据库被 {' 和 '.join(parts)} 引用，请先删除或改绑相关配置"
        )

    name = record.name
    db.delete(record)
    log_operation(db, current_user, "删除数据库", "database", database_id, name,
                  ip_address=client_ip(request))
    db.commit()
    return MessageResponse(message="删除成功")
