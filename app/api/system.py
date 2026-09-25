"""
系统 API：状态、配置、主题、日志文件读取。
"""
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.database import get_db
from ..core.errors import BadRequestError, NotFoundError
from ..models.database import (
    BackupHistory,
    BackupPlan,
    Database,
    SyncTask,
    SystemConfig,
    User,
)
from ..services.audit import log_operation
from .deps import get_current_user

router = APIRouter()

# 允许通过 API 读写的配置键白名单。旧实现允许写任意 key，
# 包括 theme_scheme / contrast_level 这些已废弃的 MD3 残留。
EDITABLE_CONFIG_KEYS = {"backup_dir", "primary_color", "dark_mode"}

DEFAULT_THEME = {
    "primary_color": "#2f6feb",
    "dark_mode": False,
}


# ============================================================ 模型

class StatusResponse(BaseModel):
    app_name: str
    app_version: str
    uptime_seconds: float
    system_uptime_seconds: float
    database_count: int
    sync_tasks_count: int
    sync_running_count: int
    backup_plans_count: int
    last_backup_time: Optional[datetime] = None
    disk_usage: Dict[str, Any] = {}
    backup_dir: str
    mysql_tools: Dict[str, Optional[str]] = {}


class SystemConfigResponse(BaseModel):
    key: str
    value: Optional[str] = None
    description: Optional[str] = None


class SystemConfigUpdate(BaseModel):
    value: str
    description: Optional[str] = None


class ThemeConfig(BaseModel):
    primary_color: str = "#2f6feb"
    dark_mode: bool = False


class ThemeUpdate(BaseModel):
    primary_color: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    dark_mode: Optional[bool] = None


class ThemeResponse(BaseModel):
    message: str
    theme: ThemeConfig


class LogsResponse(BaseModel):
    logs: List[str]
    total_lines: int


# ============================================================ 状态

@router.get("/status", response_model=StatusResponse)
async def get_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StatusResponse:
    """系统概况。"""
    from ..services.backup import backup_manager
    from ..services.mysql.tools import mysql_tools_available
    from ..services.sync import sync_manager

    last_backup = (
        db.query(BackupHistory)
        .filter(BackupHistory.status == "completed" if False else True)
        .order_by(BackupHistory.created_at.desc())
        .first()
    )

    disk_usage: Dict[str, Any] = {}
    try:
        import psutil

        path = settings.BACKUP_DIR if os.path.exists(settings.BACKUP_DIR) else "."
        usage = psutil.disk_usage(path)
        disk_usage = {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": usage.percent,
        }
    except Exception:
        pass

    system_uptime = 0.0
    try:
        import psutil

        system_uptime = max(0.0, time.time() - psutil.boot_time())
    except Exception:
        pass

    return StatusResponse(
        app_name=settings.APP_NAME,
        app_version=settings.APP_VERSION,
        uptime_seconds=_process_uptime(),
        system_uptime_seconds=system_uptime,
        database_count=db.query(Database).count(),
        sync_tasks_count=db.query(SyncTask).count(),
        sync_running_count=sync_manager.running_count,
        backup_plans_count=db.query(BackupPlan).count(),
        last_backup_time=last_backup.created_at if last_backup else None,
        disk_usage=disk_usage,
        backup_dir=os.path.abspath(settings.BACKUP_DIR),
        mysql_tools=mysql_tools_available(),
    )


_PROCESS_START = time.time()


def _process_uptime() -> float:
    return max(0.0, time.time() - _PROCESS_START)


# ============================================================ 配置

@router.get("/configs", response_model=List[SystemConfigResponse])
async def list_configs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[SystemConfigResponse]:
    rows = db.query(SystemConfig).order_by(SystemConfig.key).all()
    return [
        SystemConfigResponse(key=row.key, value=row.value, description=row.description)
        for row in rows
    ]


@router.get("/configs/{key}", response_model=SystemConfigResponse)
async def get_config(
    key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SystemConfigResponse:
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if row is None:
        raise NotFoundError("配置项不存在")
    return SystemConfigResponse(key=row.key, value=row.value, description=row.description)


@router.put("/configs/{key}", response_model=SystemConfigResponse)
async def update_config(
    key: str,
    payload: SystemConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SystemConfigResponse:
    """更新配置项。仅允许白名单内的键。"""
    if key not in EDITABLE_CONFIG_KEYS:
        raise BadRequestError(
            f"配置项「{key}」不允许通过接口修改。"
            f"可修改项: {', '.join(sorted(EDITABLE_CONFIG_KEYS))}"
        )

    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if row is None:
        row = SystemConfig(key=key, value=payload.value, description=payload.description)
        db.add(row)
    else:
        row.value = payload.value
        if payload.description is not None:
            row.description = payload.description

    log_operation(db, current_user, "更新系统配置", "system_config", None, key, payload.value)
    db.commit()
    db.refresh(row)
    return SystemConfigResponse(key=row.key, value=row.value, description=row.description)


# ============================================================ 主题

@router.get("/theme", response_model=ThemeConfig)
async def get_theme(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ThemeConfig:
    """主题配置。仅保留主色与深浅色——旧的 HCT 色域算法生成 32 个 token 的方案已废弃。"""
    rows = (
        db.query(SystemConfig)
        .filter(SystemConfig.key.in_(["primary_color", "dark_mode"]))
        .all()
    )
    theme = dict(DEFAULT_THEME)
    for row in rows:
        if row.key == "primary_color" and row.value:
            theme["primary_color"] = row.value
        elif row.key == "dark_mode" and row.value:
            theme["dark_mode"] = row.value.lower() == "true"
    return ThemeConfig(**theme)


@router.put("/theme", response_model=ThemeResponse)
async def update_theme(
    payload: ThemeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ThemeResponse:
    """更新主题配置。"""
    updates: Dict[str, str] = {}
    if payload.primary_color is not None:
        updates["primary_color"] = payload.primary_color
    if payload.dark_mode is not None:
        updates["dark_mode"] = "true" if payload.dark_mode else "false"

    if not updates:
        raise BadRequestError("未提供任何可更新的主题字段")

    for key, value in updates.items():
        row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        if row is None:
            db.add(SystemConfig(key=key, value=value, description=f"主题配置 - {key}"))
        else:
            row.value = value

    log_operation(db, current_user, "更新主题", "system_config", None, "theme",
                  ", ".join(f"{k}={v}" for k, v in updates.items()))
    db.commit()

    return ThemeResponse(message="主题配置更新成功", theme=await get_theme(db, current_user))


# ============================================================ 日志文件

@router.get("/logs", response_model=LogsResponse)
async def read_log_file(
    lines: int = Query(200, ge=1, le=5000),
    level: Optional[str] = Query(default=None, max_length=20),
    keyword: Optional[str] = Query(default=None, max_length=200),
    current_user: User = Depends(get_current_user),
) -> LogsResponse:
    """
    读取应用日志文件尾部。

    日志文件可能很大，因此从尾部按块读取，而不是把整个文件载入内存
    （旧实现用 readlines() 全量读入后再切片）。
    """
    path = settings.LOG_FILE
    if not os.path.exists(path):
        return LogsResponse(logs=[], total_lines=0)

    tail = _tail_lines(path, max(lines * 4, lines))
    total = len(tail)

    if level:
        needle = level.upper()
        tail = [ln for ln in tail if needle in ln.upper()]
    if keyword:
        tail = [ln for ln in tail if keyword in ln]

    return LogsResponse(logs=tail[-lines:], total_lines=total)


def _tail_lines(path: str, count: int, block_size: int = 8192) -> List[str]:
    """从文件尾部读取最多 count 行。"""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        data = b""
        read = 0
        while read < size and data.count(b"\n") <= count:
            step = min(block_size, size - read)
            read += step
            f.seek(size - read)
            data = f.read(step) + data
    return data.decode("utf-8", errors="replace").splitlines()


# ============================================================ OpenList 上传配置

class OpenListConfigResponse(BaseModel):
    """OpenList 配置（不含密码明文）。"""

    enabled: bool = False
    base_url: str = ""
    username: str = ""
    remote_dir: str = "/"
    verify_ssl: bool = True
    password_set: bool = False


class OpenListConfigUpdate(BaseModel):
    """更新 OpenList 配置。password 留空表示不修改已保存的密码。"""

    enabled: bool = False
    base_url: str = Field(default="", max_length=500)
    username: str = Field(default="", max_length=200)
    password: Optional[str] = Field(default=None, max_length=500)
    remote_dir: str = Field(default="/", max_length=500)
    verify_ssl: bool = True


class OpenListTestRequest(BaseModel):
    """连接测试。可只测已保存的配置，也可用表单里尚未保存的值测试。"""

    base_url: Optional[str] = Field(default=None, max_length=500)
    username: Optional[str] = Field(default=None, max_length=200)
    password: Optional[str] = Field(default=None, max_length=500)
    remote_dir: Optional[str] = Field(default=None, max_length=500)
    verify_ssl: Optional[bool] = None


class OpenListTestResponse(BaseModel):
    success: bool
    message: str
    directory: Optional[str] = None


@router.get("/openlist", response_model=OpenListConfigResponse)
async def get_openlist_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OpenListConfigResponse:
    """读取 OpenList 上传配置。"""
    from ..services import openlist

    return OpenListConfigResponse(**openlist.describe_config())


@router.put("/openlist", response_model=OpenListConfigResponse)
async def update_openlist_config(
    payload: OpenListConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OpenListConfigResponse:
    """保存 OpenList 上传配置。密码加密存储。"""
    from ..services import openlist

    if payload.enabled and not payload.base_url.strip():
        raise BadRequestError("启用上传前必须填写 OpenList 服务地址")

    openlist.save_config(
        enabled=payload.enabled,
        base_url=payload.base_url,
        username=payload.username,
        password=payload.password,
        remote_dir=payload.remote_dir,
        verify_ssl=payload.verify_ssl,
    )

    log_operation(
        db, current_user, "更新 OpenList 配置", "system_config", None, "openlist",
        f"启用={payload.enabled} 地址={payload.base_url} 目录={payload.remote_dir}",
    )
    db.commit()

    return OpenListConfigResponse(**openlist.describe_config())


@router.post("/openlist/test", response_model=OpenListTestResponse)
async def test_openlist_connection(
    payload: OpenListTestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OpenListTestResponse:
    """
    测试 OpenList 连接：登录并确认目标目录可用。

    表单里填了值就用表单的值（尚未保存也能先验证）；
    没填的字段回退到已保存的配置，密码留空时用已保存的密码。
    """
    import asyncio

    from ..services import openlist
    from ..services.openlist.client import OpenListClient, OpenListConfig, OpenListError

    try:
        stored = openlist.load_config(require_password=False)
    except RuntimeError as exc:
        # 密码解密失败等配置问题，直接给出可操作的提示
        return OpenListTestResponse(success=False, message=str(exc))

    base_url = (payload.base_url or (stored.base_url if stored else "") or "").strip()
    username = (payload.username or (stored.username if stored else "") or "").strip()
    remote_dir = (payload.remote_dir or (stored.remote_dir if stored else "") or "/").strip()
    verify_ssl = payload.verify_ssl if payload.verify_ssl is not None else (
        stored.verify_ssl if stored else True
    )

    # 密码优先用表单填写的，留空则用已保存的
    password = payload.password or (stored.password if stored else "") or ""

    if not base_url:
        return OpenListTestResponse(success=False, message="请填写 OpenList 服务地址")
    if not username:
        return OpenListTestResponse(success=False, message="请填写用户名")
    if not password:
        return OpenListTestResponse(success=False, message="请填写密码")

    client = OpenListClient(
        OpenListConfig(
            base_url=base_url,
            username=username,
            password=password,
            remote_dir=remote_dir,
            verify_ssl=verify_ssl,
        )
    )

    try:
        info = await asyncio.to_thread(client.test_connection)
    except OpenListError as exc:
        return OpenListTestResponse(success=False, message=str(exc))
    except Exception as exc:
        logger.error(f"OpenList 连接测试异常: {type(exc).__name__}: {exc}")
        return OpenListTestResponse(
            success=False, message=f"连接测试失败：{type(exc).__name__} - {exc}"
        )

    return OpenListTestResponse(
        success=True,
        message=f"连接成功，目标目录可用：{info.get('directory')}",
        directory=info.get("directory"),
    )
