"""
备份服务
"""
import os
import subprocess
import shutil
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from loguru import logger
import schedule
import time
import glob
from croniter import croniter

from ..core.database import SessionLocal
from ..core.config import settings, now_beijing
from ..core.crypto import decrypt
from ..models.database import BackupPlan, BackupHistory, BackupStatus, BackupType, Database, RunLog


def _find_mysql_tool(tool_name: str) -> str:
    """查找 MySQL 客户端工具的完整路径，支持 macOS Homebrew 等非标准安装"""
    # 先尝试 shutil.which（使用当前进程的 PATH）
    found = shutil.which(tool_name)
    if found:
        return found
    
    # macOS Homebrew 常见路径
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
    
    # 找不到，返回原始名称让 subprocess 报错
    logger.warning(f"找不到 {tool_name}，尝试使用默认命令名")
    return tool_name


class MySQLBackup:
    """MySQL备份实现"""
    
    def __init__(self, plan_id: int, database_config: dict, backup_config: dict):
        self.plan_id = plan_id
        self.database_config = database_config
        self.backup_config = backup_config
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.scheduler = schedule.Scheduler()

    def _test_connection(self):
        """测试数据库连接"""
        import mysql.connector
        try:
            conn = mysql.connector.connect(
                host=self.database_config['host'],
                port=self.database_config['port'],
                user=self.database_config['username'],
                password=self.database_config['password'],
                database=self.database_config['database'],
                ssl_disabled=True  # 禁用 SSL（避免自签名证书问题）
            )
            conn.close()
        except Exception as e:
            raise Exception(f"数据库连接失败: {e}")
        
    def start(self):
        """启动备份计划"""
        if self.running:
            return
        
        self.running = True
        
        # 设置定时任务
        self._setup_schedule()
        
        # 启动调度线程
        self.thread = threading.Thread(target=self._schedule_loop, daemon=True)
        self.thread.start()
        
        logger.info(f"备份计划 {self.plan_id} 已启动")
    
    def stop(self):
        """停止备份计划"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info(f"备份计划 {self.plan_id} 已停止")
    
    def _setup_schedule(self):
        """设置定时任务"""
        interval = self.backup_config.get('schedule_interval')
        cron = self.backup_config.get('schedule_cron')

        if interval:
            # 使用间隔调度
            self.scheduler.every(interval).minutes.do(self._execute_backup)
        elif cron:
            # 使用 croniter 解析 cron 表达式，计算下次运行时间
            if croniter.is_valid(cron):
                now = datetime.now()
                cron_itr = croniter(cron, now)
                next_run = cron_itr.get_next(datetime)
                delay_seconds = (next_run - now).total_seconds()
                # 使用 schedule 在计算出的时间点运行
                self.scheduler.every(delay_seconds).seconds.do(self._execute_and_reschedule)
                self._cron_expr = cron
                self._last_scheduled_run = now  # 记录调度基准时间
                logger.info(f"备份计划 {self.plan_id} cron='{cron}' 下次运行: {next_run}")
            else:
                logger.error(f"备份计划 {self.plan_id} cron 表达式无效: {cron}")

    def _execute_and_reschedule(self):
        """执行备份后重新调度下一次（用于 cron 模式，避免时间漂移）"""
        self._execute_backup()
        # 基于上次调度时间计算下一次运行，避免因备份耗时而漂移
        if hasattr(self, '_cron_expr'):
            # 使用上次调度时间计算下次运行，而非当前时间
            base_time = self._last_scheduled_run if hasattr(self, '_last_scheduled_run') else datetime.now()
            cron_itr = croniter(self._cron_expr, base_time)
            next_run = cron_itr.get_next(datetime)
            # 如果计算出的下次时间已过（可能性很小），用当前时间重算
            if next_run <= datetime.now():
                cron_itr = croniter(self._cron_expr, datetime.now())
                next_run = cron_itr.get_next(datetime)
            delay_seconds = max(1, (next_run - datetime.now()).total_seconds())
            self._last_scheduled_run = next_run
            self.scheduler.clear()
            self.scheduler.every(delay_seconds).seconds.do(self._execute_and_reschedule)
            logger.info(f"备份计划 {self.plan_id} 下次运行: {next_run}")
    
    def _schedule_loop(self):
        """调度循环"""
        while self.running:
            self.scheduler.run_pending()
            time.sleep(1)
    
    def _execute_backup(self):
        """执行备份"""
        error_detail = None
        try:
            logger.info(f"开始执行备份计划 {self.plan_id}")
            self._write_run_log("INFO", f"开始执行备份计划")

            # 创建备份历史记录
            history_id = self._create_backup_history()

            # 执行备份
            if self.backup_config['backup_type'] == BackupType.FULL:
                success, error_detail = self._full_backup(history_id)
            else:
                success, error_detail = self._incremental_backup(history_id)

            # 更新备份历史（包含错误详情）
            self._update_backup_history(history_id, success, error_detail)

            # 清理过期备份
            self._cleanup_old_backups()

            if success:
                logger.info(f"备份计划 {self.plan_id} 执行完成")
                self._write_run_log("INFO", "备份执行成功")
            else:
                logger.error(f"备份计划 {self.plan_id} 执行失败: {error_detail}")
                self._write_run_log("ERROR", "备份执行失败", detail=error_detail)

        except Exception as e:
            error_detail = str(e)
            logger.error(f"备份计划 {self.plan_id} 执行异常: {e}")
            self._write_run_log("ERROR", f"备份执行异常: {error_detail}", detail=error_detail)
    
    def _create_backup_history(self) -> int:
        """创建备份历史记录"""
        db = SessionLocal()
        try:
            history = BackupHistory(
                backup_plan_id=self.plan_id,
                status=BackupStatus.RUNNING,
                backup_type=self.backup_config['backup_type'],
                start_time=now_beijing()
            )
            db.add(history)
            db.commit()
            db.refresh(history)
            return history.id
        finally:
            db.close()
    
    def _update_backup_history(self, history_id: int, success: bool, error_message: str = None):
        """更新备份历史记录"""
        db = SessionLocal()
        try:
            history = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
            if history:
                history.end_time = now_beijing()
                if history.start_time:
                    history.duration = int((history.end_time - history.start_time).total_seconds())
                
                if success:
                    history.status = BackupStatus.COMPLETED
                    # 获取文件大小
                    if history.file_path and os.path.exists(history.file_path):
                        history.file_size = os.path.getsize(history.file_path)
                else:
                    history.status = BackupStatus.FAILED
                    history.error_message = error_message
                
                db.commit()
        finally:
            db.close()
    
    def _full_backup(self, history_id: int) -> tuple:
        """执行全量备份，返回 (success: bool, error_message: Optional[str])"""
        try:
            # 生成备份文件名
            timestamp = now_beijing().strftime("%Y%m%d_%H%M%S")
            filename = f"full_backup_{self.database_config['database']}_{timestamp}.sql"
            backup_path = os.path.join(self.backup_config['backup_dir'], filename)
            
            # 确保备份目录存在
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            
            # 构建mysqldump命令（密码通过环境变量传递，避免在进程列表中暴露）
            mysqldump_path = _find_mysql_tool('mysqldump')
            cmd = [
                mysqldump_path,
                f'--host={self.database_config["host"]}',
                f'--port={self.database_config["port"]}',
                f'--user={self.database_config["username"]}',
                '--single-transaction',
                '--routines',
                '--triggers',
                '--events',
                '--ssl-mode=DISABLED',          # 跳过 SSL（兼容新旧 MySQL 客户端）
                self.database_config['database']
            ]
            
            dump_env = {**os.environ, 'MYSQL_PWD': self.database_config['password']}
            
            # 执行备份
            with open(backup_path, 'w') as f:
                result = subprocess.run(
                    cmd,
                    stdout=f,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=3600,  # 1小时超时
                    env=dump_env
                )
            
            if result.returncode == 0:
                # 更新历史记录
                db = SessionLocal()
                history = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
                if history:
                    history.file_path = backup_path
                    db.commit()
                db.close()
                
                logger.info(f"全量备份成功: {backup_path}")
                return True, None
            else:
                error_msg = result.stderr.strip() if result.stderr else f"mysqldump 退出码: {result.returncode}"
                logger.error(f"全量备份失败: {error_msg}")
                return False, error_msg
                
        except Exception as e:
            logger.error(f"全量备份异常: {e}")
            return False, str(e)
    
    def _incremental_backup(self, history_id: int) -> tuple:
        """执行增量备份（基于 binlog），返回 (success: bool, error_message: Optional[str])"""
        try:
            timestamp = now_beijing().strftime("%Y%m%d_%H%M%S")
            filename = f"incremental_backup_{self.database_config['database']}_{timestamp}.sql"
            backup_path = os.path.join(self.backup_config['backup_dir'], filename)
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)

            # 获取所有 binlog 文件列表（覆盖 binlog 轮转场景）
            binlog_files = self._get_all_binlog_files()
            if not binlog_files:
                error_msg = "无法获取 binlog 文件列表，请确认源数据库已启用 binlog 且连接正常"
                logger.error(error_msg)
                return False, error_msg

            # 查找上次全量/增量备份的时间作为起始时间
            start_time = self._get_last_backup_time()
            stop_time = now_beijing()

            mysqlbinlog_path = _find_mysql_tool('mysqlbinlog')
            backup_env = {**os.environ, 'MYSQL_PWD': self.database_config['password']}

            # 遍历所有 binlog 文件，逐个导出时间范围内的变更
            backup_success = True
            backup_error = None
            first_file = True
            for binlog_file in binlog_files:
                cmd = [
                    mysqlbinlog_path,
                    f'--host={self.database_config["host"]}',
                    f'--port={self.database_config["port"]}',
                    f'--user={self.database_config["username"]}',
                    '--read-from-remote-server',
                    '--ssl-mode=DISABLED',
                    f'--start-datetime={start_time.strftime("%Y-%m-%d %H:%M:%S")}',
                    f'--stop-datetime={stop_time.strftime("%Y-%m-%d %H:%M:%S")}',
                    binlog_file
                ]

                mode = 'w' if first_file else 'a'
                first_file = False
                with open(backup_path, mode) as f:
                    result = subprocess.run(
                        cmd, stdout=f, stderr=subprocess.PIPE,
                        text=True, timeout=3600, env=backup_env
                    )
                if result.returncode != 0:
                    error_msg = result.stderr.strip() if result.stderr else f"mysqlbinlog 退出码: {result.returncode}"
                    logger.warning(f"binlog 文件 {binlog_file} 导出有警告: {error_msg}")
                    backup_error = error_msg
                    backup_success = False

            if backup_success:
                db = SessionLocal()
                history = db.query(BackupHistory).filter(BackupHistory.id == history_id).first()
                if history:
                    history.file_path = backup_path
                    db.commit()
                db.close()
                logger.info(f"增量备份成功: {backup_path}")
                return True, None
            else:
                logger.error(f"增量备份完成但有错误: {backup_error}")
                return False, backup_error

        except Exception as e:
            logger.error(f"增量备份异常: {e}")
            return False, str(e)

    def _get_all_binlog_files(self) -> list:
        """获取所有 binlog 文件列表（兼容 MySQL 5.7/8.0/8.4+），按时间排序"""
        try:
            import mysql.connector
            conn = mysql.connector.connect(
                host=self.database_config['host'],
                port=self.database_config['port'],
                user=self.database_config['username'],
                password=self.database_config['password'],
                database=self.database_config['database']
            )
            cursor = conn.cursor()
            cursor.execute("SHOW BINARY LOGS")
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            if rows:
                return [row[0] for row in rows]
            # SHOW BINARY LOGS 不可用，回退到当前文件
            fallback = self._get_current_binlog_file()
            return [fallback] if fallback else []
        except Exception as e:
            logger.warning(f"获取 binlog 文件列表失败: {e}")
            fallback = self._get_current_binlog_file()
            return [fallback] if fallback else []

    def _get_current_binlog_file(self) -> Optional[str]:
        """获取当前 binlog 文件名（兼容 MySQL 5.7/8.0/8.4+）"""
        try:
            import mysql.connector
            conn = mysql.connector.connect(
                host=self.database_config['host'],
                port=self.database_config['port'],
                user=self.database_config['username'],
                password=self.database_config['password'],
                database=self.database_config['database']
            )
            cursor = conn.cursor()
            
            # MySQL 8.4+ 移除了 SHOW MASTER STATUS，使用 SHOW BINARY LOG STATUS
            # MySQL 8.0.22 引入了 SHOW BINARY LOG STATUS 但仍支持旧命令
            # MySQL 5.7 只支持 SHOW MASTER STATUS
            row = None
            for sql in ("SHOW BINARY LOG STATUS", "SHOW MASTER STATUS"):
                try:
                    cursor.execute(sql)
                    row = cursor.fetchone()
                    if row:
                        break
                except Exception:
                    continue
            
            cursor.close()
            conn.close()
            if row:
                return row[0]  # binlog_file
            return None
        except Exception as e:
            logger.error(f"获取 binlog 文件名失败: {e}")
            return None

    def _get_last_backup_time(self) -> datetime:
        """获取该计划上一次成功备份的时间，用于增量备份起始时间"""
        try:
            db = SessionLocal()
            last = db.query(BackupHistory).filter(
                BackupHistory.backup_plan_id == self.plan_id,
                BackupHistory.status == BackupStatus.COMPLETED
            ).order_by(BackupHistory.end_time.desc()).first()
            db.close()
            if last and last.end_time:
                return last.end_time
        except Exception as e:
            logger.warning(f"查询上次备份时间失败: {e}")
        # 默认回退到1小时前
        return now_beijing() - timedelta(hours=1)

    def _cleanup_old_backups(self):
        """清理超出保留天数的过期备份文件和记录"""
        retention_days = self.backup_config.get('retention_days', 30)
        if not retention_days or retention_days <= 0:
            return

        cutoff = now_beijing() - timedelta(days=retention_days)
        db = SessionLocal()
        try:
            old_records = db.query(BackupHistory).filter(
                BackupHistory.backup_plan_id == self.plan_id,
                BackupHistory.status == BackupStatus.COMPLETED,
                BackupHistory.end_time < cutoff
            ).all()

            for record in old_records:
                # 删除备份文件
                if record.file_path and os.path.exists(record.file_path):
                    try:
                        os.remove(record.file_path)
                        logger.info(f"已删除过期备份文件: {record.file_path}")
                    except OSError as e:
                        logger.warning(f"删除备份文件失败: {record.file_path} - {e}")
                # 删除数据库记录
                db.delete(record)

            if old_records:
                db.commit()
                logger.info(f"备份计划 {self.plan_id} 已清理 {len(old_records)} 条过期记录")
        except Exception as e:
            logger.error(f"清理过期备份失败: {e}")
        finally:
            db.close()

    def _write_run_log(self, level: str, message: str, detail: str = None):
        """写入运行日志"""
        try:
            db = SessionLocal()
            plan = db.query(BackupPlan).filter(BackupPlan.id == self.plan_id).first()
            plan_name = plan.name if plan else f"Plan#{self.plan_id}"
            log = RunLog(
                task_type="backup",
                task_id=self.plan_id,
                task_name=plan_name,
                level=level,
                message=message,
                detail=detail
            )
            db.add(log)
            db.commit()
            db.close()
        except Exception as e:
            logger.error(f"写入运行日志失败: {e}")


class BackupService:
    """备份服务管理"""
    
    def __init__(self):
        self.backup_plans: Dict[int, MySQLBackup] = {}
        self.running = False
    
    async def start(self):
        """启动备份服务"""
        self.running = True
        logger.info("备份服务已启动")
        
        # 加载并启动所有活跃的备份计划
        await self._load_active_plans()
    
    async def stop(self):
        """停止备份服务"""
        self.running = False
        
        # 停止所有备份计划
        for plan_id, backup in self.backup_plans.items():
            backup.stop()
        
        self.backup_plans.clear()
        logger.info("备份服务已停止")
    
    async def _load_active_plans(self):
        """加载活跃的备份计划"""
        db = SessionLocal()
        try:
            plans = db.query(BackupPlan).filter(
                BackupPlan.is_active == True
            ).all()
            
            logger.info(f"发现 {len(plans)} 个活跃的备份计划")
            
            for plan in plans:
                try:
                    await self.start_plan(plan.id)
                except Exception as e:
                    logger.error(f"加载备份计划 {plan.id} ({plan.name}) 失败: {e}")
                    # 继续加载其他计划，不因单个失败而中断
                    continue
            
        except Exception as e:
            logger.error(f"查询活跃备份计划失败: {e}")
        finally:
            db.close()
    
    async def start_plan(self, plan_id: int):
        """启动备份计划"""
        # 检查是否真正在运行（不只是字典中存在）
        if plan_id in self.backup_plans and self.backup_plans[plan_id].running:
            logger.warning(f"备份计划 {plan_id} 已在运行中，跳过启动")
            return
        
        db = SessionLocal()
        try:
            plan = db.query(BackupPlan).filter(BackupPlan.id == plan_id).first()
            if not plan:
                raise Exception(f"备份计划 {plan_id} 不存在")
            
            logger.info(f"正在启动备份计划 {plan_id} ({plan.name})...")
            
            # 获取数据库配置
            database = db.query(Database).filter(Database.id == plan.database_id).first()
            if not database:
                raise Exception(f"关联的数据库 (ID={plan.database_id}) 不存在")
            
            # 创建备份实例
            database_config = {
                'host': database.host,
                'port': database.port,
                'username': database.username,
                'password': decrypt(database.password),
                'database': database.database_name
            }
            
            backup_config = {
                'backup_type': plan.backup_type,
                'schedule_cron': plan.schedule_cron,
                'schedule_interval': plan.schedule_interval,
                'backup_dir': plan.backup_path or settings.BACKUP_DIR,
                'retention_days': plan.retention_days
            }
            
            logger.info(f"备份计划 {plan_id} 配置: type={plan.backup_type}, interval={plan.schedule_interval}, cron={plan.schedule_cron}, dir={backup_config['backup_dir']}")
            
            backup = MySQLBackup(plan_id, database_config, backup_config)
            self.backup_plans[plan_id] = backup
            
            # 启动备份计划
            backup.start()
            
            logger.info(f"备份计划 {plan_id} ({plan.name}) 启动成功")
            
        except Exception as e:
            logger.error(f"启动备份计划 {plan_id} 失败: {e}")
            # 从运行列表中移除
            if plan_id in self.backup_plans:
                del self.backup_plans[plan_id]
            raise
        finally:
            db.close()
    
    async def stop_plan(self, plan_id: int):
        """停止备份计划"""
        if plan_id not in self.backup_plans:
            logger.warning(f"备份计划 {plan_id} 未在运行")
            return
        
        try:
            backup = self.backup_plans[plan_id]
            backup.stop()
            del self.backup_plans[plan_id]
            
            logger.info(f"备份计划 {plan_id} 停止成功")
            
        except Exception as e:
            logger.error(f"停止备份计划 {plan_id} 失败: {e}")
            raise
    
    async def execute_backup(self, plan_id: int):
        """立即执行备份（即使计划未在调度中也允许手动执行）"""
        if plan_id in self.backup_plans:
            backup = self.backup_plans[plan_id]
            backup._test_connection()
            backup._execute_backup()
            return
        
        # 计划未在运行中，临时加载并执行一次备份
        try:
            db = SessionLocal()
            plan = db.query(BackupPlan).filter(BackupPlan.id == plan_id).first()
            if not plan:
                raise Exception(f"备份计划 {plan_id} 不存在")

            database = db.query(Database).filter(Database.id == plan.database_id).first()
            if not database:
                raise Exception("关联的数据库不存在")

            database_config = {
                'host': database.host,
                'port': database.port,
                'username': database.username,
                'password': decrypt(database.password),
                'database': database.database_name
            }
            backup_config = {
                'backup_type': plan.backup_type,
                'backup_dir': plan.backup_path or settings.BACKUP_DIR,
                'retention_days': plan.retention_days
            }
            db.close()

            backup = MySQLBackup(plan_id, database_config, backup_config)
            backup._test_connection()
            backup._execute_backup()
            logger.info(f"手动执行备份计划 {plan_id} 完成")

        except Exception as e:
            logger.error(f"手动执行备份计划 {plan_id} 失败: {e}")
            # 写入运行日志
            self._write_run_log_to_db(plan_id, "ERROR", f"手动执行备份失败: {str(e)}")
            raise

    def _write_run_log_to_db(self, plan_id: int, level: str, message: str):
        """写入运行日志到数据库"""
        try:
            db = SessionLocal()
            plan = db.query(BackupPlan).filter(BackupPlan.id == plan_id).first()
            plan_name = plan.name if plan else f"Plan#{plan_id}"
            log = RunLog(
                task_type="backup",
                task_id=plan_id,
                task_name=plan_name,
                level=level,
                message=message
            )
            db.add(log)
            db.commit()
            db.close()
        except Exception as e:
            logger.error(f"写入运行日志失败: {e}")
    
    def get_plan_status(self, plan_id: int) -> Optional[Dict]:
        """获取备份计划状态"""
        if plan_id in self.backup_plans:
            backup = self.backup_plans[plan_id]
            return {
                "plan_id": plan_id,
                "running": backup.running,
                "status": "running" if backup.running else "stopped"
            }
        return None