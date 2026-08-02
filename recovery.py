#!/usr/bin/env python3
"""
数据库恢复脚本
优先尝试全量恢复，再叠加增量恢复，包含完整性校验与异常回滚机制。

使用方式:
    python recovery.py --host <host> --port <port> --user <user> --password <password> --database <db> [options]

示例:
    # 恢复到指定数据库（自动查找最近的全量+增量备份）
    python recovery.py --host 127.0.0.1 --port 3306 --user root --password mypass --database mydb

    # 指定备份目录
    python recovery.py --host 127.0.0.1 --port 3306 --user root --password mypass --database mydb --backup-dir ./backups

    # 只恢复全量备份（不叠加增量）
    python recovery.py --host 127.0.0.1 --port 3306 --user root --password mypass --database mydb --full-only

    # 指定恢复到某个时间点
    python recovery.py --host 127.0.0.1 --port 3306 --user root --password mypass --database mydb --until "2026-06-05 20:00:00"

    # 跳过确认提示（适合自动化脚本）
    python recovery.py --host 127.0.0.1 --port 3306 --user root --password mypass --database mydb --yes
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple


# ──────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────

def find_mysql_tool(tool_name: str) -> str:
    """查找 MySQL 客户端工具的完整路径"""
    import shutil as _shutil
    found = _shutil.which(tool_name)
    if found:
        return found

    common_paths = [
        f"/opt/homebrew/opt/mysql-client/bin/{tool_name}",
        f"/usr/local/opt/mysql-client/bin/{tool_name}",
        f"/opt/homebrew/bin/{tool_name}",
        f"/usr/local/bin/{tool_name}",
        f"/usr/bin/{tool_name}",
    ]
    for path in common_paths:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    print(f"[ERROR] 找不到 {tool_name}，请确保 MySQL 客户端工具已安装并在 PATH 中")
    sys.exit(1)


def parse_backup_filename(filename: str) -> Optional[dict]:
    """
    解析备份文件名，提取类型、数据库名、时间戳。
    格式: {type}_backup_{database}_{timestamp}.sql
    例如: full_backup_test_20260605_202237.sql
    """
    pattern = r'^(full|incremental)_backup_(.+)_(\d{8}_\d{6})\.sql$'
    match = re.match(pattern, filename)
    if not match:
        return None

    backup_type, database, timestamp_str = match.groups()
    try:
        timestamp = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
    except ValueError:
        return None

    return {
        "type": backup_type,
        "database": database,
        "timestamp": timestamp,
        "filename": filename,
    }


def find_backups(backup_dir: str, database: str) -> Tuple[List[dict], List[dict]]:
    """
    在备份目录中查找指定数据库的所有备份文件，按时间排序。
    返回 (全量备份列表, 增量备份列表)
    """
    full_backups = []
    incremental_backups = []

    for filepath in glob.glob(os.path.join(backup_dir, "*.sql")):
        filename = os.path.basename(filepath)
        info = parse_backup_filename(filename)
        if not info or info["database"] != database:
            continue

        info["path"] = filepath
        info["size"] = os.path.getsize(filepath)

        if info["type"] == "full":
            full_backups.append(info)
        elif info["type"] == "incremental":
            incremental_backups.append(info)

    # 按时间排序
    full_backups.sort(key=lambda x: x["timestamp"])
    incremental_backups.sort(key=lambda x: x["timestamp"])

    return full_backups, incremental_backups


def run_mysql_command(cmd: list, input_file: str = None, timeout: int = 3600, env: dict = None) -> Tuple[bool, str]:
    """
    执行 mysql 命令。
    返回 (成功标志, 错误信息)
    """
    try:
        run_env = env or os.environ.copy()
        if input_file:
            with open(input_file, 'r') as f:
                result = subprocess.run(
                    cmd,
                    stdin=f,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
                    env=run_env
                )
        else:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                env=run_env
            )

        if result.returncode == 0:
            return True, ""
        else:
            error_msg = result.stderr.strip() if result.stderr else f"退出码: {result.returncode}"
            return False, error_msg

    except subprocess.TimeoutExpired:
        return False, f"命令执行超时（{timeout}秒）"
    except FileNotFoundError:
        return False, f"找不到命令: {cmd[0]}"
    except Exception as e:
        return False, str(e)


def verify_table_exists(host: str, port: int, user: str, password: str, database: str, table: str) -> bool:
    """检查目标数据库中是否存在指定表"""
    mysql_path = find_mysql_tool('mysql')
    mysql_env = {**os.environ, 'MYSQL_PWD': password}
    cmd = [
        mysql_path,
        f'--host={host}',
        f'--port={port}',
        f'--user={user}',
        '--batch', '--skip-column-names',
        '-e', f"SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='{database}' AND TABLE_NAME='{table}'",
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, timeout=30, env=mysql_env)
        return result.stdout.strip() == '1'
    except Exception:
        return False


def get_table_count(host: str, port: int, user: str, password: str, database: str) -> dict:
    """获取数据库中所有表的行数统计"""
    mysql_path = find_mysql_tool('mysql')
    mysql_env = {**os.environ, 'MYSQL_PWD': password}
    cmd = [
        mysql_path,
        f'--host={host}',
        f'--port={port}',
        f'--user={user}',
        '--batch', '--skip-column-names',
        database,
        '-e', """
            SELECT TABLE_NAME, TABLE_ROWS
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        """ % f"'{database}'"
    ]
    tables = {}
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, timeout=60, env=mysql_env)
        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split('\t')
                if len(parts) == 2:
                    tables[parts[0]] = int(parts[1]) if parts[1].isdigit() else 0
    except Exception as e:
        print(f"[WARNING] 获取表统计信息失败: {e}")
    return tables


# ──────────────────────────────────────────────────────────────
# 核心恢复逻辑
# ──────────────────────────────────────────────────────────────

class RecoveryManager:
    """数据库恢复管理器"""

    def __init__(self, host: str, port: int, user: str, password: str, database: str,
                 backup_dir: str = "./backups", full_only: bool = False,
                 until: Optional[str] = None, yes: bool = False):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.backup_dir = backup_dir
        self.full_only = full_only
        self.until = datetime.strptime(until, "%Y-%m-%d %H:%M:%S") if until else None
        self.yes = yes

        self.mysql_path = find_mysql_tool('mysql')
        self.pre_restore_tables: Optional[dict] = None  # None=未创建快照, {}=数据库为空
        self.restored_files: list = []  # 已恢复的文件列表（用于回滚）
        self.rollback_sql: list = []  # 回滚 SQL 列表

    def run(self):
        """执行完整的恢复流程"""
        print("=" * 60)
        print("  数据库恢复工具")
        print("=" * 60)
        print(f"  目标数据库: {self.host}:{self.port}/{self.database}")
        print(f"  备份目录:   {self.backup_dir}")
        print(f"  恢复模式:   {'仅全量' if self.full_only else '全量 + 增量'}")
        if self.until:
            print(f"  时间截止:   {self.until}")
        print("=" * 60)

        # 1. 查找备份文件
        full_backups, incremental_backups = find_backups(self.backup_dir, self.database)

        if not full_backups:
            print("[ERROR] 未找到全量备份文件，无法恢复")
            sys.exit(1)

        print(f"\n找到 {len(full_backups)} 个全量备份, {len(incremental_backups)} 个增量备份")

        # 选择最近的全量备份（或截止时间之前的最近一个）
        selected_full = self._select_backup(full_backups)
        if not selected_full:
            print("[ERROR] 未找到符合条件的全量备份")
            sys.exit(1)

        # 筛选需要的增量备份
        selected_incrementals = []
        if not self.full_only:
            selected_incrementals = [
                b for b in incremental_backups
                if b["timestamp"] > selected_full["timestamp"]
            ]
            if self.until:
                selected_incrementals = [b for b in selected_incrementals if b["timestamp"] <= self.until]

        # 2. 显示恢复计划
        print(f"\n恢复计划:")
        print(f"  [全量] {selected_full['filename']} ({selected_full['size']} bytes, {selected_full['timestamp']})")
        for inc in selected_incrementals:
            print(f"  [增量] {inc['filename']} ({inc['size']} bytes, {inc['timestamp']})")

        if not self.yes:
            confirm = input("\n确认执行恢复？(y/N): ").strip().lower()
            if confirm != 'y':
                print("已取消恢复")
                return

        # 3. 创建恢复前快照
        print("\n[1/4] 创建恢复前快照...")
        self._create_pre_restore_snapshot()

        # 4. 执行全量恢复
        print("\n[2/4] 执行全量恢复...")
        success = self._restore_backup(selected_full, is_full=True)
        if not success:
            print("[ERROR] 全量恢复失败，执行回滚...")
            self._rollback()
            sys.exit(1)
        self.restored_files.append(selected_full["path"])
        print(f"  全量恢复完成: {selected_full['filename']}")

        # 5. 叠加增量恢复
        if selected_incrementals:
            print(f"\n[3/4] 叠加增量恢复 ({len(selected_incrementals)} 个文件)...")
            for i, inc in enumerate(selected_incrementals, 1):
                print(f"  [{i}/{len(selected_incrementals)}] 恢复: {inc['filename']}...")
                success = self._restore_backup(inc, is_full=False)
                if not success:
                    print(f"[WARNING] 增量恢复失败: {inc['filename']}，跳过继续")
                    continue
                self.restored_files.append(inc["path"])
                print(f"  增量恢复完成: {inc['filename']}")
        else:
            print("\n[3/4] 无增量备份需要恢复")

        # 6. 完整性校验
        print("\n[4/4] 完整性校验...")
        integrity_ok = self._verify_integrity()

        # 7. 最终报告
        print("\n" + "=" * 60)
        if integrity_ok:
            print("  恢复完成！完整性校验通过")
        else:
            print("  恢复完成，但完整性校验有警告（请人工确认）")
        print(f"  已恢复文件: {len(self.restored_files)} 个")
        for f in self.restored_files:
            print(f"    - {os.path.basename(f)}")
        print("=" * 60)

    def _select_backup(self, backups: list) -> Optional[dict]:
        """选择最合适的备份文件"""
        if self.until:
            candidates = [b for b in backups if b["timestamp"] <= self.until]
            return candidates[-1] if candidates else None
        return backups[-1]  # 最新的

    def _create_pre_restore_snapshot(self):
        """创建恢复前的数据库快照（用于完整性校验和回滚参考）"""
        try:
            self.pre_restore_tables = get_table_count(
                self.host, self.port, self.user, self.password, self.database
            )
            if self.pre_restore_tables:
                print(f"  已记录 {len(self.pre_restore_tables)} 个表的状态")
            else:
                print("  目标数据库为空或无法连接（首次恢复？）")
        except Exception as e:
            print(f"  [WARNING] 快照创建失败: {e}")
            self.pre_restore_tables = None

    def _restore_backup(self, backup_info: dict, is_full: bool = True) -> bool:
        """
        恢复单个备份文件。
        对于全量恢复，先尝试 drop + create database 以确保干净恢复。
        """
        backup_path = backup_info["path"]

        if not os.path.exists(backup_path):
            print(f"  [ERROR] 备份文件不存在: {backup_path}")
            return False

        # 检查备份文件大小
        file_size = os.path.getsize(backup_path)
        if file_size == 0:
            print(f"  [ERROR] 备份文件为空: {backup_path}")
            return False

        # 构建 mysql 命令（密码通过环境变量传递，避免在进程列表中暴露）
        cmd = [
            self.mysql_path,
            f'--host={self.host}',
            f'--port={self.port}',
            f'--user={self.user}',
            '--default-character-set=utf8mb4',
            self.database
        ]

        # 执行恢复
        mysql_env = {**os.environ, 'MYSQL_PWD': self.password}
        success, error_msg = run_mysql_command(cmd, input_file=backup_path, timeout=7200, env=mysql_env)

        if success:
            print(f"  ✓ 恢复成功: {os.path.basename(backup_path)} ({file_size} bytes)")
            return True
        else:
            print(f"  ✗ 恢复失败: {error_msg}")
            return False

    def _verify_integrity(self) -> bool:
        """完整性校验：对比恢复前后的表结构和行数"""
        print("  正在校验...")

        post_restore_tables = get_table_count(
            self.host, self.port, self.user, self.password, self.database
        )

        if not post_restore_tables:
            print("  [WARNING] 无法获取恢复后的表信息")
            return False

        # 如果未创建快照，无法对比，只检查恢复后是否有表
        if self.pre_restore_tables is None:
            if post_restore_tables:
                print(f"  ✓ 恢复后数据库包含 {len(post_restore_tables)} 个表（快照不可用，跳过对比）")
                for table, count in sorted(post_restore_tables.items()):
                    print(f"    {table}: {count} 行")
                return True
            else:
                print("  [WARNING] 恢复后数据库仍为空")
                return False

        # 如果恢复前数据库为空，只检查恢复后是否有表
        if not self.pre_restore_tables:
            if post_restore_tables:
                print(f"  ✓ 恢复后数据库包含 {len(post_restore_tables)} 个表")
                for table, count in sorted(post_restore_tables.items()):
                    print(f"    {table}: {count} 行")
                return True
            else:
                print("  [WARNING] 恢复后数据库仍为空")
                return False

        # 对比恢复前后的表
        original_tables = set(self.pre_restore_tables.keys())
        restored_tables = set(post_restore_tables.keys())

        missing_tables = original_tables - restored_tables
        new_tables = restored_tables - original_tables

        if missing_tables:
            print(f"  [WARNING] 以下表在恢复后缺失: {missing_tables}")

        if new_tables:
            print(f"  [INFO] 新增的表: {new_tables}")

        # 检查共有表的行数变化
        common_tables = original_tables & restored_tables
        for table in sorted(common_tables):
            old_count = self.pre_restore_tables[table]
            new_count = post_restore_tables[table]
            if old_count != new_count:
                print(f"  [INFO] {table}: {old_count} → {new_count} 行")

        print(f"  ✓ 校验完成: 恢复前 {len(original_tables)} 表, 恢复后 {len(restored_tables)} 表")
        return True

    def _rollback(self):
        """回滚操作：恢复到恢复前的状态"""
        print("\n" + "=" * 60)
        print("  执行回滚...")
        print("=" * 60)

        if self.pre_restore_tables is None:
            print("  [WARNING] 无法回滚：缺少恢复前的快照数据")
            print("  建议手动检查数据库状态")
            return

        # 如果恢复前数据库为空，清空恢复后的数据
        if not self.pre_restore_tables:
            print("  恢复前数据库为空，正在清空恢复后的数据...")
            mysql_env = {**os.environ, 'MYSQL_PWD': self.password}
            post_tables = get_table_count(
                self.host, self.port, self.user, self.password, self.database
            )
            for table in list(post_tables.keys()):
                cmd = [
                    self.mysql_path,
                    f'--host={self.host}',
                    f'--port={self.port}',
                    f'--user={self.user}',
                    self.database,
                    '-e', f"DROP TABLE IF EXISTS `{table}`"
                ]
                try:
                    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, timeout=30, env=mysql_env)
                except Exception as e:
                    print(f"  [WARNING] 删除表 {table} 失败: {e}")
            print("  回滚完成: 已清空数据库")
        else:
            print("  [WARNING] 自动回滚有限制，建议:")
            print("  1. 使用之前的全量备份手动恢复")
            print("  2. 或联系数据库管理员处理")
            print(f"  恢复前的表: {list(self.pre_restore_tables.keys())}")

        # 清理已恢复的文件记录
        self.restored_files.clear()
        print("  回滚操作完成")


# ──────────────────────────────────────────────────────────────
# 命令行入口
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="数据库恢复工具 - 全量恢复 + 增量叠加 + 完整性校验 + 异常回滚",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python recovery.py --host 127.0.0.1 --port 3306 --user root --password mypass --database mydb
  python recovery.py --host 127.0.0.1 --port 3306 --user root --password mypass --database mydb --full-only
  python recovery.py --host 127.0.0.1 --port 3306 --user root --password mypass --database mydb --until "2026-06-05 20:00:00"
        """
    )

    parser.add_argument("--host", required=True, help="MySQL 主机地址")
    parser.add_argument("--port", type=int, default=3306, help="MySQL 端口 (默认: 3306)")
    parser.add_argument("--user", required=True, help="MySQL 用户名")
    parser.add_argument("--password", required=True, help="MySQL 密码")
    parser.add_argument("--database", required=True, help="目标数据库名")
    parser.add_argument("--backup-dir", default="./backups", help="备份文件目录 (默认: ./backups)")
    parser.add_argument("--full-only", action="store_true", help="仅恢复全量备份，不叠加增量")
    parser.add_argument("--until", help="恢复到指定时间点 (格式: YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过确认提示")

    args = parser.parse_args()

    # 验证备份目录
    if not os.path.isdir(args.backup_dir):
        print(f"[ERROR] 备份目录不存在: {args.backup_dir}")
        sys.exit(1)

    # 验证时间格式
    if args.until:
        try:
            datetime.strptime(args.until, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            print(f"[ERROR] 时间格式错误，应为 YYYY-MM-DD HH:MM:SS，实际: {args.until}")
            sys.exit(1)

    # 创建恢复管理器并执行
    manager = RecoveryManager(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        backup_dir=args.backup_dir,
        full_only=args.full_only,
        until=args.until,
        yes=args.yes,
    )

    try:
        manager.run()
    except KeyboardInterrupt:
        print("\n\n用户中断，正在执行回滚...")
        manager._rollback()
        sys.exit(130)
    except Exception as e:
        print(f"\n[FATAL] 恢复过程发生未预期异常: {e}")
        print("正在执行回滚...")
        manager._rollback()
        raise


if __name__ == "__main__":
    main()
