"""
后台服务注册表。

main.py 的 lifespan 通过这里启停所有后台服务，保持依赖方向单一：
main → services → core，不存在回路。
"""
from .backup import backup_manager
from .sync import sync_manager

__all__ = ["sync_manager", "backup_manager"]
