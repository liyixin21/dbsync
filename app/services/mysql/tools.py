"""
MySQL 客户端可执行文件的定位与子进程调用。

路径探测逻辑原本散落在 backup_service 与 sync_tasks 中，现集中于此。
所有调用均为异步（asyncio.create_subprocess_exec），避免阻塞事件循环——
旧实现在 async 路由里用 subprocess.run(timeout=3600)，一次全量复制会冻住整个服务。
"""
import asyncio
import os
import shutil
from dataclasses import dataclass
from typing import Dict, List, Optional

from loguru import logger

# macOS Homebrew / Linux 常见安装位置
_COMMON_PREFIXES = (
    "/opt/homebrew/opt/mysql-client/bin",
    "/usr/local/opt/mysql-client/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/usr/local/mysql/bin",
)


def find_mysql_tool(tool_name: str) -> str:
    """
    定位 MySQL 客户端工具。

    先查 PATH，再逐个尝试常见安装前缀；都找不到时返回原始名称，
    由子进程抛出可读错误。
    """
    found = shutil.which(tool_name)
    if found:
        return found

    for prefix in _COMMON_PREFIXES:
        candidate = os.path.join(prefix, tool_name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    logger.warning(f"未找到 {tool_name}，将回退到命令名由系统解析")
    return tool_name


def mysql_tools_available() -> Dict[str, Optional[str]]:
    """启动期自检：检查 mysqldump / mysql 是否可用。"""
    result: Dict[str, Optional[str]] = {}
    for tool in ("mysqldump", "mysql"):
        path = find_mysql_tool(tool)
        # 回退到裸命令名说明没找到
        result[tool] = path if os.path.isabs(path) else None
    return result


@dataclass
class CommandResult:
    """子进程执行结果。"""

    returncode: int
    stderr: str
    duration: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def error_summary(self) -> str:
        text = (self.stderr or "").strip()
        if not text:
            return f"退出码 {self.returncode}"
        # MySQL 客户端可能输出多行警告，取信息量最大的部分
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return " | ".join(lines[-3:])[:800]


def _env_with_password(password: str) -> Dict[str, str]:
    """通过 MYSQL_PWD 传密码，避免出现在进程列表里。"""
    env = dict(os.environ)
    env["MYSQL_PWD"] = password
    return env


def build_dump_command(
    *,
    host: str,
    port: int,
    user: str,
    database: str,
    routines: bool = True,
    triggers: bool = True,
    events: bool = True,
) -> List[str]:
    """构造 mysqldump 命令。"""
    cmd = [
        find_mysql_tool("mysqldump"),
        f"--host={host}",
        f"--port={port}",
        f"--user={user}",
        "--single-transaction",
        "--skip-lock-tables",
        "--hex-blob",
        "--default-character-set=utf8mb4",
        "--complete-insert",
    ]
    if routines:
        cmd.append("--routines")
    if triggers:
        cmd.append("--triggers")
    if events:
        cmd.append("--events")
    cmd.append(database)
    return cmd


def build_import_command(*, host: str, port: int, user: str, database: str) -> List[str]:
    """构造 mysql 导入命令。导入前关闭外键与唯一性检查以规避表顺序依赖。"""
    return [
        find_mysql_tool("mysql"),
        f"--host={host}",
        f"--port={port}",
        f"--user={user}",
        "--default-character-set=utf8mb4",
        "--force",
        "--init-command=SET FOREIGN_KEY_CHECKS=0, UNIQUE_CHECKS=0",
        database,
    ]


async def run_dump_to_file(
    cmd: List[str],
    *,
    password: str,
    output_path: str,
    timeout: int,
) -> CommandResult:
    """执行 mysqldump 并写入文件。"""
    started = asyncio.get_running_loop().time()
    with open(output_path, "wb") as out:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=out,
            stderr=asyncio.subprocess.PIPE,
            env=_env_with_password(password),
        )
        try:
            _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return CommandResult(-1, f"导出超时（{timeout}s）", timeout)
    duration = asyncio.get_running_loop().time() - started
    return CommandResult(
        proc.returncode or 0,
        (stderr_b or b"").decode("utf-8", errors="replace"),
        duration,
    )


async def run_import_from_file(
    cmd: List[str],
    *,
    password: str,
    input_path: str,
    timeout: int,
) -> CommandResult:
    """执行 mysql < file 导入。"""
    started = asyncio.get_running_loop().time()
    with open(input_path, "rb") as src:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=src,
            stderr=asyncio.subprocess.PIPE,
            env=_env_with_password(password),
        )
        try:
            _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return CommandResult(-1, f"导入超时（{timeout}s）", timeout)
    duration = asyncio.get_running_loop().time() - started
    return CommandResult(
        proc.returncode or 0,
        (stderr_b or b"").decode("utf-8", errors="replace"),
        duration,
    )


def verify_dump_file(path: str) -> tuple[bool, str]:
    """
    校验 dump 文件完整性。

    mysqldump 正常结束时会写入 `-- Dump completed` 尾部标记；
    进程被中断时文件会残留但缺少该标记——这类半截文件绝不能当作有效备份。
    """
    if not os.path.exists(path):
        return False, "备份文件不存在"
    size = os.path.getsize(path)
    if size == 0:
        return False, "备份文件为空"

    # 从尾部读取，避免大文件全量载入
    tail_size = min(4096, size)
    with open(path, "rb") as f:
        f.seek(-tail_size, os.SEEK_END)
        tail = f.read().decode("utf-8", errors="replace")

    if "Dump completed" not in tail:
        return False, f"备份文件不完整（缺少结束标记，已写入 {size} 字节）"

    return True, ""
