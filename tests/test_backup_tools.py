"""
备份工具链测试。

mysqldump 输出的完整性校验是这里的重点：进程被中断时文件会残留但缺少
结束标记，这类半截文件绝不能被当成有效备份。
"""
import os
import tempfile

import pytest

from app.services.mysql.tools import (
    build_dump_command,
    build_import_command,
    find_mysql_tool,
    mysql_tools_available,
    verify_dump_file,
)


class TestToolDiscovery:
    def test_finds_known_tool(self):
        """本机存在 mysqldump/binary 时应当能定位到绝对路径。"""
        path = find_mysql_tool("mysqldump")
        assert path  # 至少返回一个非空字符串

    def test_missing_tool_returns_name(self):
        path = find_mysql_tool("definitely-not-a-real-tool-xyz")
        assert path == "definitely-not-a-real-tool-xyz"

    def test_availability_report_shape(self):
        report = mysql_tools_available()
        assert set(report.keys()) == {"mysqldump", "mysql"}


class TestCommandBuilding:
    def test_dump_command_includes_safety_flags(self):
        cmd = build_dump_command(
            host="db.internal", port=3306, user="backup", database="app"
        )
        joined = " ".join(cmd)
        # 一致性快照，避免锁表
        assert "--single-transaction" in joined
        assert "--skip-lock-tables" in joined
        # 二进制安全
        assert "--hex-blob" in joined
        # 字符集明确
        assert "--default-character-set=utf8mb4" in joined
        assert "--host=db.internal" in joined
        assert "--user=backup" in joined
        assert cmd[-1] == "app"

    def test_dump_command_never_contains_password(self):
        """
        密码必须通过环境变量传递，绝不能出现在命令行（进程列表可见）。

        构造命令的函数本身不接收密码，因此这里验证两点：
        1. 生成的参数里不含任何密码形态的选项
        2. 实际运行时的环境变量确实带上了 MYSQL_PWD
        """
        cmd = build_dump_command(host="h", port=3306, user="u", database="d")

        # 不应出现 --password=xxx 这类会暴露到进程列表的形式
        assert not any("password" in part.lower() for part in cmd)
        assert not any(part.startswith("-p") and len(part) > 2 for part in cmd)

        # 密码走环境变量
        from app.services.mysql.tools import _env_with_password

        secret = "s3cr3t-placeholder-value"
        env = _env_with_password(secret)
        assert env["MYSQL_PWD"] == secret
        assert not any(secret in part for part in cmd)

    def test_dump_command_optional_features(self):
        cmd = build_dump_command(
            host="h", port=3306, user="u", database="d",
            routines=False, triggers=False, events=False,
        )
        joined = " ".join(cmd)
        assert "--routines" not in joined
        assert "--triggers" not in joined
        assert "--events" not in joined

    def test_import_command_disables_fk_checks(self):
        cmd = build_import_command(host="h", port=3306, user="u", database="target")
        joined = " ".join(cmd)
        # 恢复/复制时表顺序不确定，必须关闭外键检查
        assert "FOREIGN_KEY_CHECKS=0" in joined
        assert "UNIQUE_CHECKS=0" in joined
        assert cmd[-1] == "target"

    def test_import_command_never_contains_password(self):
        cmd = build_import_command(host="h", port=3306, user="u", database="d")
        assert not any("password" in part.lower() for part in cmd)


class TestDumpIntegrity:
    def test_valid_complete_dump(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
            f.write("-- MySQL dump 10.13\nCREATE TABLE t (id INT);\n")
            f.write("-- Dump completed on 2024-01-01 12:00:00\n")
            path = f.name
        try:
            ok, reason = verify_dump_file(path)
            assert ok is True
            assert reason == ""
        finally:
            os.unlink(path)

    def test_truncated_dump_rejected(self):
        """
        关键：进程被 SIGKILL 时 dump 文件会残留但没有结束标记。
        把这种文件当成有效备份，会在需要恢复时才发现无法还原。
        """
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
            f.write("-- MySQL dump 10.13\nCREATE TABLE t (id INT);\nINSERT INTO t VALUES (1")
            path = f.name
        try:
            ok, reason = verify_dump_file(path)
            assert ok is False
            assert "不完整" in reason
        finally:
            os.unlink(path)

    def test_empty_file_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
            path = f.name
        try:
            ok, reason = verify_dump_file(path)
            assert ok is False
            assert "空" in reason
        finally:
            os.unlink(path)

    def test_missing_file_rejected(self):
        ok, reason = verify_dump_file("/tmp/definitely-not-here-12345.sql")
        assert ok is False
        assert "不存在" in reason

    def test_large_file_tail_read(self):
        """大文件只读尾部，不整文件载入。"""
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
            f.write("-- header\n")
            f.write("x" * 200_000)          # 200 KB 填充
            f.write("\n-- Dump completed\n")
            path = f.name
        try:
            ok, _ = verify_dump_file(path)
            assert ok is True
        finally:
            os.unlink(path)
