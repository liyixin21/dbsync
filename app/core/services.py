"""
全局服务实例注册表
避免 API 层和 main.py 之间的循环导入
"""
from ..services.sync_service import SyncService
from ..services.backup_service import BackupService

sync_service = SyncService()
backup_service = BackupService()
