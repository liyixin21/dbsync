"""
FastAPI 应用入口。

装配顺序：配置校验 → 日志 → 数据库 → 后台服务 → 路由 → 异常处理器。
所有异常出口统一为 {"error": {"code", "message", "detail"}}。
"""
import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import api_router
from .core.config import settings
from .core.database import SessionLocal, check_database, engine, init_db
from .core.errors import AppError

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DIST_DIR = os.path.join(STATIC_DIR, "dist")
INDEX_FILE = os.path.join(DIST_DIR, "index.html")


def _serve_index():
    """返回前端入口；未构建时给出可操作的提示。"""
    if os.path.exists(INDEX_FILE):
        return FileResponse(INDEX_FILE, media_type="text/html")
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "code": "FRONTEND_NOT_BUILT",
                "message": "前端尚未构建。请在 frontend/ 目录执行 npm install && npm run build",
            }
        },
    )


# ============================================================ 日志

def _check_writable(directory: str) -> bool:
    """检测目录是否可写。"""
    try:
        os.makedirs(directory, exist_ok=True)
        probe = os.path.join(directory, ".write-probe")
        with open(probe, "w") as fh:
            fh.write("")
        os.unlink(probe)
        return True
    except OSError:
        return False


def _configure_logging() -> None:
    """
    配置日志。

    文件日志失败**不会**导致启动中止：控制台输出才是容器运维的主要入口，
    因挂载目录权限问题（宿主机目录属主与容器内 UID 不一致）而拒绝启动，
    代价远大于失去文件日志。此时会打印清晰的排查指引。
    """
    log_dir = os.path.dirname(os.path.abspath(settings.LOG_FILE))
    os.makedirs(log_dir, exist_ok=True)

    logger.remove()
    logger.add(
        sink=lambda msg: print(msg, end=""),
        level=settings.LOG_LEVEL,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
               "<level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - {message}",
        colorize=True,
    )

    if not _check_writable(log_dir):
        uid = os.getuid() if hasattr(os, "getuid") else "?"
        gid = os.getgid() if hasattr(os, "getgid") else "?"
        logger.warning(
            f"日志目录不可写，已跳过文件日志：{log_dir}\n"
            f"  当前进程 UID:GID = {uid}:{gid}\n"
            "  常见原因：容器以非 root 用户运行，而挂载的宿主机目录属主不同。\n"
            "  修复方式（在宿主机执行其一）：\n"
            f"    sudo chown -R {uid}:{gid} ./data ./backups\n"
            "    或在 compose 中为该服务指定 user: \"root\""
        )
        return

    try:
        logger.add(
            settings.LOG_FILE,
            rotation="10 MB",
            retention="30 days",
            compression="gz",
            encoding="utf-8",
            level=settings.LOG_LEVEL,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
            enqueue=True,
            backtrace=True,
            diagnose=False,
        )
    except (OSError, PermissionError) as exc:
        # 兜底：即便探测通过，实际创建文件仍可能失败（如磁盘只读）
        logger.warning(f"无法写入日志文件 {settings.LOG_FILE}：{exc}（已仅使用控制台日志）")


# ============================================================ 后台服务协调

async def _health_watchdog() -> None:
    """周期性刷新同步任务指标，并监控后台异常。"""
    from .services.sync import sync_manager

    while True:
        try:
            await asyncio.sleep(5)
            await sync_manager.refresh_health()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"健康检查循环异常: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    settings.validate_for_startup()

    logger.info(f"启动 {settings.APP_NAME} v{settings.APP_VERSION}")

    # 路径可写性自检：把权限问题连同修复命令在启动时讲清楚。
    # 这类问题在挂载卷场景很常见（宿主机目录属主与容器内 UID 不一致），
    # 若不提前检查，会以难以定位的写入错误出现在运行中途。
    from .core.database import check_writable_paths

    unwritable = check_writable_paths()
    if unwritable:
        uid = os.getuid() if hasattr(os, "getuid") else "?"
        gid = os.getgid() if hasattr(os, "getgid") else "?"
        details = "\n".join(f"    {label} {path} — {err}" for label, path, err in unwritable)
        raise RuntimeError(
            "以下目录不可写，服务无法启动：\n"
            f"{details}\n\n"
            f"  当前进程 UID:GID = {uid}:{gid}\n"
            "  常见原因：容器以非 root 用户运行，而挂载的宿主机目录属主不同。\n"
            "  修复方式（在宿主机执行其一）：\n"
            f"    sudo chown -R {uid}:{gid} ./data ./backups\n"
            '    或在 compose 中为该服务指定 user: "root"'
        )

    init_db()
    check_database()
    _bootstrap_admin()
    _credential_self_check()

    from .services.audit import run_log_buffer
    from .services.backup import backup_manager
    from .services.sync import sync_manager

    run_log_buffer.start()

    watchdog = asyncio.create_task(_health_watchdog(), name="health-watchdog")
    await sync_manager.start()
    await backup_manager.start()

    logger.info(
        f"服务就绪 http://{settings.HOST}:{settings.PORT} | "
        f"备份目录 {os.path.abspath(settings.BACKUP_DIR)}"
    )

    try:
        yield
    finally:
        logger.info("正在关闭服务...")
        watchdog.cancel()
        try:
            await watchdog
        except (asyncio.CancelledError, Exception):
            pass

        await sync_manager.stop()
        await backup_manager.stop()
        run_log_buffer.stop()
        engine.dispose()
        logger.info("服务已关闭")


def _bootstrap_admin() -> None:
    """仅在没有任何用户时创建默认管理员。幂等，绝不重置已有密码。"""
    from .core.security import hash_password
    from .models.database import User

    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            return
        db.add(
            User(username="admin", password_hash=hash_password("admin123"), is_active=True)
        )
        db.commit()
        logger.warning("已创建默认管理员 admin / admin123 —— 请立即修改密码")
    except Exception as exc:
        logger.error(f"创建默认管理员失败: {exc}")
    finally:
        db.close()


def _credential_self_check() -> None:
    """
    启动期校验存量数据库凭据能否解密。

    SECRET_KEY 变更后旧密文无法解开；与其等到用户点「启动同步」时才发现，
    不如启动时就把出问题的数据库名列出来。
    """
    from .core.crypto import CredentialDecryptError, decrypt
    from .models.database import Database

    db = SessionLocal()
    try:
        rows = db.query(Database).all()
        broken = []
        for row in rows:
            try:
                decrypt(row.password)
            except CredentialDecryptError:
                broken.append(row.name)
        if broken:
            logger.error(
                f"以下数据库的密码无法解密，请重新录入: {', '.join(broken)}"
                "（通常由 SECRET_KEY 变更引起）"
            )
    except Exception as exc:
        logger.error(f"凭据自检失败: {exc}")
    finally:
        db.close()


# ============================================================ 应用

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="基于 MySQL binlog 的实时数据库同步与全量备份工具",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

# CORS 默认关闭：本应用是同源部署的单页应用，旧实现 allow_origins=["*"] 没有必要。
# 需要跨域时通过 CORS_ORIGINS 环境变量显式声明白名单。
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

app.include_router(api_router, prefix="/api")


# ============================================================ 异常处理器

@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    """业务异常：统一为 error 外壳。"""
    if exc.status_code >= 500:
        logger.error(f"{request.method} {request.url.path} → {exc.code}: {exc.message}")
    return JSONResponse(status_code=exc.status_code, content={"error": exc.to_payload()})


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求参数校验失败。"""
    details = []
    for item in exc.errors():
        loc = " → ".join(str(part) for part in item.get("loc", [])[1:]) or "body"
        details.append({"field": loc, "message": item.get("msg", "参数无效")})

    first = details[0]["message"] if details else "请求参数无效"
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"参数校验失败: {first}",
                "detail": details,
            }
        },
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Starlette/FastAPI 内置 HTTP 异常，转成统一外壳。"""
    message = exc.detail if isinstance(exc.detail, str) else "请求失败"
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": f"HTTP_{exc.status_code}", "message": message}},
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """兜底：未预期异常记完整堆栈，对外只给通用信息。"""
    logger.exception(f"未处理异常 {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "服务器内部错误，请查看日志获取详情",
            }
        },
    )


# ============================================================ 静态资源与首页

@app.get("/health", tags=["系统"])
async def health_check() -> dict:
    """健康检查。"""
    return {"status": "healthy"}


if os.path.isdir(DIST_DIR):
    # 前端构建产物由 Vite 输出到 static/dist，按 /assets 前缀挂载
    assets_dir = os.path.join(DIST_DIR, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


@app.get("/", include_in_schema=False)
async def index():
    """返回前端入口。"""
    return _serve_index()


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """站点图标。未提供时返回 204 而不是 404，避免浏览器控制台噪声。"""
    path = os.path.join(DIST_DIR, "favicon.ico")
    if os.path.exists(path):
        return FileResponse(path)
    return Response(status_code=204)


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    """
    SPA 前端路由回退。

    前端使用 history 模式，「/databases」「/sync」等路径由 Vue Router 处理。
    用户在子页面刷新或粘贴链接时浏览器会请求该路径——若服务端只注册了 "/"，
    就会返回 404，用户看到的是一页 JSON 错误而不是应用界面。

    必须放在最后注册：FastAPI 按声明顺序匹配，/api 与 /health 等
    具体路由先命中，只有未匹配的路径才会落到这里。
    """
    # /api 下的未知路径应返回 JSON 404，不能回退成 HTML
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")

    # 缺失的静态资源同样不回退：返回 HTML 会让浏览器按 JS/CSS 解析而报错
    if "." in full_path.rsplit("/", 1)[-1]:
        raise HTTPException(status_code=404, detail="Not Found")

    return _serve_index()
