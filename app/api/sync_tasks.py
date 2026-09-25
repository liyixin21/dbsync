"""
同步任务 API。

新增端点：
- GET  /sync-tasks/{id}/tables          每表同步状态
- GET  /sync-tasks/{id}/errors          DLQ 列表
- POST /sync-tasks/{id}/errors/{eid}/retry  重放单条失败事件
- DEL  /sync-tasks/{id}/errors          清空 DLQ

这些是旧实现最缺的东西：此前一张表被静默放弃后，前端完全无从知晓。
"""
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..core.errors import BadRequestError, NotFoundError, OperationFailedError
from ..core.pagination import Page, PageMeta, paginate
from ..models.database import (
    Database,
    SyncError,
    SyncHealth,
    SyncStatus,
    SyncTableState,
    SyncTask,
    User,
)
from ..services.audit import log_operation, log_run_detached
from ..services.sync import sync_manager
from ..services.sync.applier import ColumnMapper
from .deps import client_ip, get_current_user

router = APIRouter()


# ============================================================ 模型

class SyncTaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    source_db_id: int
    target_db_id: int
    auto_start: bool = False


class SyncTaskUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    source_db_id: Optional[int] = None
    target_db_id: Optional[int] = None
    auto_start: Optional[bool] = None


class SyncTaskResponse(BaseModel):
    id: int
    name: str
    source_db_id: int
    target_db_id: int
    source_db_name: Optional[str] = None
    target_db_name: Optional[str] = None
    status: str
    health: str
    error_message: Optional[str] = None
    auto_start: bool

    gtid_set: Optional[str] = None
    binlog_file: Optional[str] = None
    binlog_position: Optional[int] = None
    binlog_format: Optional[str] = None
    binlog_row_image: Optional[str] = None

    last_sync_time: Optional[datetime] = None
    sync_delay: int = 0
    applied_events: int = 0
    dlq_count: int = 0
    running: bool = False

    created_at: datetime
    updated_at: datetime


class TableStateResponse(BaseModel):
    schema_name: str
    table_name: str
    state: str
    pk_columns: Optional[Any] = None
    unique_keys: Optional[Any] = None
    last_binlog_file: Optional[str] = None
    last_binlog_pos: Optional[int] = None
    last_applied_at: Optional[datetime] = None
    applied_events: int = 0
    error_count: int = 0
    last_error: Optional[str] = None


class SyncErrorResponse(BaseModel):
    id: int
    schema_name: str
    table_name: str
    event_type: str
    error_code: Optional[int] = None
    error_message: str
    payload: Optional[str] = None
    binlog_file: Optional[str] = None
    binlog_pos: Optional[int] = None
    retry_count: int = 0
    resolved: bool = False
    created_at: datetime


class MessageResponse(BaseModel):
    message: str
    task_id: Optional[int] = None


class FullCopyResponse(BaseModel):
    message: str
    source: str
    target: str
    file_size: int
    duration: int


# ============================================================ 辅助

def _to_response(task: SyncTask, db: Session) -> SyncTaskResponse:
    source = db.query(Database).filter(Database.id == task.source_db_id).first()
    target = db.query(Database).filter(Database.id == task.target_db_id).first()
    return SyncTaskResponse(
        id=task.id,
        name=task.name,
        source_db_id=task.source_db_id,
        target_db_id=task.target_db_id,
        source_db_name=source.name if source else None,
        target_db_name=target.name if target else None,
        status=task.status.value if task.status else "pending",
        health=task.health.value if task.health else "unknown",
        error_message=task.error_message,
        auto_start=bool(task.auto_start),
        gtid_set=task.gtid_set,
        binlog_file=task.binlog_file,
        binlog_position=task.binlog_position,
        binlog_format=task.binlog_format,
        binlog_row_image=task.binlog_row_image,
        last_sync_time=task.last_sync_time,
        sync_delay=task.sync_delay or 0,
        applied_events=task.applied_events or 0,
        dlq_count=task.dlq_count or 0,
        running=sync_manager.is_active(task.id),
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _get_task(db: Session, task_id: int) -> SyncTask:
    task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
    if task is None:
        raise NotFoundError("同步任务不存在")
    return task


# ============================================================ CRUD

@router.get("/", response_model=List[SyncTaskResponse])
async def list_sync_tasks(
    skip: int = 0,
    limit: int = 200,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[SyncTaskResponse]:
    """同步任务列表。"""
    query = db.query(SyncTask)
    if status:
        try:
            query = query.filter(SyncTask.status == SyncStatus(status))
        except ValueError:
            raise BadRequestError(f"未知状态: {status}")

    tasks = query.order_by(SyncTask.id).offset(max(0, skip)).limit(
        max(1, min(limit, 500))
    ).all()
    return [_to_response(task, db) for task in tasks]


@router.post("/", response_model=SyncTaskResponse)
async def create_sync_task(
    payload: SyncTaskCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SyncTaskResponse:
    """创建同步任务。"""
    source = db.query(Database).filter(Database.id == payload.source_db_id).first()
    if source is None:
        raise BadRequestError("源数据库不存在")
    target = db.query(Database).filter(Database.id == payload.target_db_id).first()
    if target is None:
        raise BadRequestError("目标数据库不存在")
    if payload.source_db_id == payload.target_db_id:
        raise BadRequestError("源数据库与目标数据库不能相同")

    existing = db.query(SyncTask).filter(SyncTask.name == payload.name).first()
    if existing is not None:
        raise BadRequestError("任务名称已存在")

    task = SyncTask(
        name=payload.name,
        source_db_id=payload.source_db_id,
        target_db_id=payload.target_db_id,
        status=SyncStatus.PENDING,
        health=SyncHealth.UNKNOWN,
        auto_start=payload.auto_start,
    )
    db.add(task)
    db.flush()

    log_operation(
        db, current_user, "创建同步任务", "sync_task", task.id, task.name,
        f"{source.name} → {target.name}", ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(task)
    return _to_response(task, db)


@router.get("/{task_id}", response_model=SyncTaskResponse)
async def get_sync_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SyncTaskResponse:
    return _to_response(_get_task(db, task_id), db)


@router.put("/{task_id}", response_model=SyncTaskResponse)
async def update_sync_task(
    task_id: int,
    payload: SyncTaskUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SyncTaskResponse:
    """更新同步任务。运行中不允许改绑数据库。"""
    task = _get_task(db, task_id)
    running = sync_manager.is_active(task_id)

    if payload.name is not None and payload.name != task.name:
        clash = (
            db.query(SyncTask)
            .filter(SyncTask.name == payload.name, SyncTask.id != task_id)
            .first()
        )
        if clash is not None:
            raise BadRequestError("任务名称已存在")
        task.name = payload.name

    if payload.source_db_id is not None or payload.target_db_id is not None:
        if running:
            raise BadRequestError("任务运行中，请先停止后再修改源/目标数据库")
        new_source = payload.source_db_id or task.source_db_id
        new_target = payload.target_db_id or task.target_db_id
        if new_source == new_target:
            raise BadRequestError("源数据库与目标数据库不能相同")
        if db.query(Database).filter(Database.id == new_source).first() is None:
            raise BadRequestError("源数据库不存在")
        if db.query(Database).filter(Database.id == new_target).first() is None:
            raise BadRequestError("目标数据库不存在")
        task.source_db_id = new_source
        task.target_db_id = new_target

    if payload.auto_start is not None:
        task.auto_start = payload.auto_start

    log_operation(db, current_user, "更新同步任务", "sync_task", task.id, task.name,
                  ip_address=client_ip(request))
    db.commit()
    db.refresh(task)
    return _to_response(task, db)


@router.delete("/{task_id}", response_model=MessageResponse)
async def delete_sync_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """删除同步任务。运行中会先停止。"""
    task = _get_task(db, task_id)
    name = task.name

    await sync_manager.remove_task(task_id)

    db.query(SyncTableState).filter(SyncTableState.task_id == task_id).delete()
    db.query(SyncError).filter(SyncError.task_id == task_id).delete()
    db.delete(task)

    log_operation(db, current_user, "删除同步任务", "sync_task", task_id, name,
                  ip_address=client_ip(request))
    db.commit()
    return MessageResponse(message="删除成功", task_id=task_id)


# ============================================================ 启停

@router.post("/{task_id}/start", response_model=MessageResponse)
async def start_sync_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """启动同步任务。启动前校验源库 binlog 配置。"""
    task = _get_task(db, task_id)
    name = task.name

    # 异常会带明确原因抛出（binlog_format 非 ROW、连接失败、凭据不可用等）
    await sync_manager.start_task(task_id)

    log_operation(db, current_user, "启动同步任务", "sync_task", task_id, name,
                  ip_address=client_ip(request))
    db.commit()
    log_run_detached("sync", task_id, name, "INFO", "同步任务已启动")
    return MessageResponse(message="任务已启动", task_id=task_id)


@router.post("/{task_id}/stop", response_model=MessageResponse)
async def stop_sync_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """停止同步任务。会等待位点落盘完成。"""
    task = _get_task(db, task_id)
    name = task.name

    await sync_manager.stop_task(task_id)

    log_operation(db, current_user, "停止同步任务", "sync_task", task_id, name,
                  ip_address=client_ip(request))
    db.commit()
    log_run_detached("sync", task_id, name, "INFO", "同步任务已停止")
    return MessageResponse(message="任务已停止", task_id=task_id)


# ============================================================ 可观测性

@router.get("/{task_id}/tables", response_model=List[TableStateResponse])
async def list_task_tables(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[TableStateResponse]:
    """每张表的同步状态与位点。"""
    _get_task(db, task_id)
    rows = (
        db.query(SyncTableState)
        .filter(SyncTableState.task_id == task_id)
        .order_by(SyncTableState.schema_name, SyncTableState.table_name)
        .all()
    )
    return [
        TableStateResponse(
            schema_name=row.schema_name,
            table_name=row.table_name,
            state=row.state.value if row.state else "active",
            pk_columns=row.pk_columns,
            unique_keys=row.unique_keys,
            last_binlog_file=row.last_binlog_file,
            last_binlog_pos=row.last_binlog_pos,
            last_applied_at=row.last_applied_at,
            applied_events=row.applied_events or 0,
            error_count=row.error_count or 0,
            last_error=row.last_error,
        )
        for row in rows
    ]


@router.get("/{task_id}/errors", response_model=Page[SyncErrorResponse])
async def list_task_errors(
    task_id: int,
    skip: int = 0,
    limit: int = 50,
    unresolved_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Page[SyncErrorResponse]:
    """失败事件队列。"""
    _get_task(db, task_id)

    query = db.query(SyncError).filter(SyncError.task_id == task_id)
    if unresolved_only:
        query = query.filter(SyncError.resolved == False)  # noqa: E712
    query = query.order_by(SyncError.id.desc())

    items, meta = paginate(query, skip, limit)
    return Page[SyncErrorResponse](
        data=[
            SyncErrorResponse(
                id=row.id,
                schema_name=row.schema_name,
                table_name=row.table_name,
                event_type=row.event_type,
                error_code=row.error_code,
                error_message=row.error_message,
                payload=row.payload,
                binlog_file=row.binlog_file,
                binlog_pos=row.binlog_pos,
                retry_count=row.retry_count or 0,
                resolved=bool(row.resolved),
                created_at=row.created_at,
            )
            for row in items
        ],
        page=meta,
    )


@router.delete("/{task_id}/errors", response_model=MessageResponse)
async def clear_task_errors(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """清空该任务的失败事件队列。"""
    _get_task(db, task_id)
    count = db.query(SyncError).filter(SyncError.task_id == task_id).delete()
    log_operation(db, current_user, "清空同步失败队列", "sync_task", task_id, None,
                  f"已清空 {count} 条", ip_address=client_ip(request))
    db.commit()
    return MessageResponse(message=f"已清空 {count} 条失败事件")


@router.post("/{task_id}/errors/{error_id}/retry", response_model=MessageResponse)
async def retry_failed_event(
    task_id: int,
    error_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    """
    重放单条失败事件。

    重放走与实时同步相同的应用器，因此同样具备幂等性。
    """
    _get_task(db, task_id)
    record = (
        db.query(SyncError)
        .filter(SyncError.id == error_id, SyncError.task_id == task_id)
        .first()
    )
    if record is None:
        raise NotFoundError("失败事件不存在")
    if record.event_type not in ("insert", "update", "delete"):
        raise BadRequestError(f"该事件类型（{record.event_type}）不支持重放")

    try:
        payload = json.loads(record.payload or "{}")
    except json.JSONDecodeError:
        raise BadRequestError("事件负载已损坏，无法重放")

    engine = sync_manager.get_engine(task_id)
    applier = getattr(engine, "_applier", None) if engine is not None else None
    if applier is None:
        raise BadRequestError("任务未在运行，请先启动任务后再重放")

    before = payload.get("before") or {}
    after = payload.get("after")

    # 事件入队时表可能还不存在，因此负载里保留的是 binlog 原始占位键
    # （UNKNOWN_COL0 之类）。此刻表已重建，可以解析出真实列名后重放；
    # 若不做这层还原，重放会生成 `INSERT INTO t (`UNKNOWN_COL0`)` 而必然失败。
    meta = applier._schema.get(record.schema_name, record.table_name)  # noqa: SLF001
    if meta is None:
        raise BadRequestError(
            f"表 {record.schema_name}.{record.table_name} 仍然不存在，无法重放"
        )

    if ColumnMapper.needs_mapping(before):
        before = ColumnMapper.remap(before, meta.columns)
    if after is not None and ColumnMapper.needs_mapping(after):
        after = ColumnMapper.remap(after, meta.columns)

    ok = False
    # 重放期间临时移除 DLQ 记录器：失败事件的处置由本端点自己负责，
    # 不应再往队列里追加新记录（那会制造「重放失败产生的失败记录」这种垃圾）。
    saved_dlq = applier._dlq  # noqa: SLF001
    applier._dlq = None  # noqa: SLF001
    try:
        if record.event_type == "insert":
            ok = applier.apply_insert(record.schema_name, record.table_name, before)
        elif record.event_type == "update":
            ok = applier.apply_update(
                record.schema_name, record.table_name, before, after or {}
            )
        elif record.event_type == "delete":
            ok = applier.apply_delete(record.schema_name, record.table_name, before)

        if ok:
            applier.commit()
        else:
            applier.rollback()
    finally:
        applier._dlq = saved_dlq  # noqa: SLF001

    record.retry_count = (record.retry_count or 0) + 1
    record.resolved = bool(ok)
    log_operation(
        db, current_user, "重放失败事件", "sync_task", task_id, None,
        f"事件 #{error_id} {'成功' if ok else '失败'}", ip_address=client_ip(request),
    )
    db.commit()

    if not ok:
        raise OperationFailedError("重放失败，请检查目标库状态")

    return MessageResponse(message="重放成功", task_id=task_id)


# ============================================================ 一键全量复制

@router.post("/{task_id}/full-copy", response_model=FullCopyResponse)
async def full_copy_database(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FullCopyResponse:
    """
    将源库全部数据完整复制到目标库。

    异步子进程执行；旧实现在 async 路由里用 subprocess.run(timeout=3600)，
    一次复制会把整个服务的事件循环冻住。
    """
    import asyncio
    import os
    import tempfile

    from ..core.config import now_beijing, settings
    from ..core.crypto import CredentialDecryptError, decrypt
    from ..services.mysql.tools import (
        build_dump_command,
        build_import_command,
        run_dump_to_file,
        run_import_from_file,
    )

    task = _get_task(db, task_id)
    source = db.query(Database).filter(Database.id == task.source_db_id).first()
    target = db.query(Database).filter(Database.id == task.target_db_id).first()
    if source is None or target is None:
        raise NotFoundError("源数据库或目标数据库不存在")

    try:
        source_password = decrypt(source.password)
        target_password = decrypt(target.password)
    except CredentialDecryptError as exc:
        raise OperationFailedError(f"数据库凭据不可用: {exc}")

    started = now_beijing()
    log_run_detached(
        "sync", task_id, task.name, "INFO",
        f"开始全量复制: {source.name} → {target.name}",
        f"源 {source.host}:{source.port}/{source.database_name} → "
        f"目标 {target.host}:{target.port}/{target.database_name}",
    )

    fd, tmp_path = tempfile.mkstemp(suffix=".sql", prefix="dbsync-fullcopy-")
    os.close(fd)

    try:
        dump_cmd = build_dump_command(
            host=source.host, port=source.port, user=source.username,
            database=source.database_name,
        )
        result = await run_dump_to_file(
            dump_cmd, password=source_password, output_path=tmp_path,
            timeout=settings.BACKUP_TIMEOUT,
        )
        if not result.ok:
            raise OperationFailedError(f"导出失败: {result.error_summary}")

        dump_size = os.path.getsize(tmp_path)
        if dump_size == 0:
            raise OperationFailedError("导出结果为空")

        import_cmd = build_import_command(
            host=target.host, port=target.port, user=target.username,
            database=target.database_name,
        )
        result = await run_import_from_file(
            import_cmd, password=target_password, input_path=tmp_path,
            timeout=settings.BACKUP_TIMEOUT,
        )
        if not result.ok:
            raise OperationFailedError(f"导入失败: {result.error_summary}")

    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass

    duration = int((now_beijing() - started).total_seconds())
    log_run_detached(
        "sync", task_id, task.name, "INFO",
        f"全量复制完成: {source.name} → {target.name}",
        f"数据量 {dump_size} 字节，耗时 {duration}s",
    )
    log_operation(
        db, current_user, "全量复制", "sync_task", task_id, task.name,
        f"{source.name} → {target.name}", ip_address=client_ip(request),
    )
    db.commit()

    return FullCopyResponse(
        message="全量复制完成", source=source.name, target=target.name,
        file_size=dump_size, duration=duration,
    )
