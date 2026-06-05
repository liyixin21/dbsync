"""
同步服务
"""
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from loguru import logger
import mysql.connector
from mysql.connector import Error
from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.row_event import WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent
from pymysqlreplication.event import QueryEvent, RotateEvent

from ..core.database import SessionLocal
from ..core.config import now_beijing
from ..core.crypto import decrypt
from ..models.database import SyncTask, SyncStatus, Database, RunLog


class MySQLBinlogSync:
    """MySQL Binlog 同步实现（基于 mysql-replication 库）"""

    def __init__(self, task_id: int, source_config: dict, target_config: dict):
        self.task_id = task_id
        self.source_config = source_config
        self.target_config = target_config
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.target_conn: Optional[mysql.connector.MySQLConnection] = None
        self.binlog_file: Optional[str] = None
        self.binlog_pos: Optional[int] = None
        self._sync_count = 0
        # 列名缓存: {(database, table): [col1, col2, ...]}
        self._column_cache: Dict[tuple, List[str]] = {}
        # 源数据库连接（用于查询列名）
        self._source_conn: Optional[mysql.connector.MySQLConnection] = None

    def _test_connections(self):
        """测试源数据库和目标数据库连接"""
        # 测试源数据库连接
        try:
            conn = mysql.connector.connect(**self.source_config)
            conn.close()
        except Error as e:
            raise Exception(f"源数据库连接失败: {e}")
        
        # 测试目标数据库连接
        try:
            conn = mysql.connector.connect(**self.target_config)
            conn.close()
        except Error as e:
            raise Exception(f"目标数据库连接失败: {e}")

    def start(self):
        """启动同步"""
        if self.running:
            return
        # 先测试数据库连接
        self._test_connections()
        self.running = True
        self.thread = threading.Thread(target=self._sync_loop, daemon=True)
        self.thread.start()
        logger.info(f"同步任务 {self.task_id} 已启动")
        self._write_run_log("INFO", "同步任务已启动")

    def stop(self):
        """停止同步"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        self._close_connections()
        logger.info(f"同步任务 {self.task_id} 已停止")
        self._write_run_log("INFO", "同步任务已停止")

    def _sync_loop(self):
        """主同步循环"""
        retry_delay = 5
        max_retry_delay = 60
        while self.running:
            try:
                self._connect_target()
                self._get_initial_position()
                self._listen_binlog()
            except Exception as e:
                logger.error(f"同步任务 {self.task_id} 发生错误: {e}")
                self._update_task_status(SyncStatus.FAILED, str(e))
                self._write_run_log("ERROR", f"同步任务发生错误: {str(e)}")
                if not self.running:
                    break
                logger.info(f"同步任务 {self.task_id} 将在 {retry_delay}s 后重试...")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)
            finally:
                self._close_connections()

    def _connect_target(self):
        """建立目标数据库连接"""
        try:
            self.target_conn = mysql.connector.connect(**self.target_config)
            logger.info(f"已连接目标数据库: {self.target_config['host']}:{self.target_config['port']}")
        except Error as e:
            raise Exception(f"目标数据库连接失败: {e}")

    def _close_connections(self):
        """关闭数据库连接"""
        try:
            if self.target_conn and self.target_conn.is_connected():
                self.target_conn.close()
        except Exception:
            pass
        try:
            if self._source_conn and self._source_conn.is_connected():
                self._source_conn.close()
        except Exception:
            pass

    def _get_column_names(self, database: str, table: str) -> List[str]:
        """从源数据库获取表的列名（带缓存）"""
        cache_key = (database, table)
        if cache_key in self._column_cache:
            return self._column_cache[cache_key]

        try:
            if not self._source_conn or not self._source_conn.is_connected():
                self._source_conn = mysql.connector.connect(**self.source_config)
            cursor = self._source_conn.cursor()
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION",
                (database, table)
            )
            columns = [row[0] for row in cursor.fetchall()]
            cursor.close()

            if columns:
                self._column_cache[cache_key] = columns
                logger.info(f"已缓存 {database}.{table} 的列名: {columns}")
            else:
                logger.warning(f"无法获取 {database}.{table} 的列名（表可能不存在）")
            return columns
        except Exception as e:
            logger.error(f"查询 {database}.{table} 列名失败: {e}")
            return []

    def _is_valid_column_dict(self, d: dict) -> bool:
        """检查字典的键是否是有效的列名（字符串），而非整数索引"""
        if not d:
            return False
        first_key = next(iter(d))
        return isinstance(first_key, str) and not first_key.startswith("UNKNOWN_COL")

    def _map_values_to_columns(self, values: dict, columns: List[str]) -> dict:
        """将可能使用整数键的值字典映射为列名键的字典"""
        if self._is_valid_column_dict(values):
            return values  # 已经是正确的列名键

        # 值字典使用整数键（0, 1, 2, ...）或 UNKNOWN_COL 前缀
        mapped = {}
        sorted_keys = sorted(values.keys(), key=lambda k: int(k) if isinstance(k, int) else int(str(k).lstrip("UNKNOWN_COL_").lstrip("UNKNOWN_COL")))
        for i, key in enumerate(sorted_keys):
            if i < len(columns):
                mapped[columns[i]] = values[key]
            else:
                mapped[f"col_{i}"] = values[key]
                logger.warning(f"列索引 {i} 超出列名范围（共 {len(columns)} 列）")
        return mapped

    def _get_initial_position(self):
        """获取初始 binlog 位置（兼容 MySQL 5.7/8.0/8.4+）"""
        try:
            conn = mysql.connector.connect(**self.source_config)
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
                except Error:
                    continue
            
            cursor.close()
            conn.close()

            if row:
                self.binlog_file = row[0]
                self.binlog_pos = row[1]
                logger.info(f"同步任务 {self.task_id} 初始位置: {self.binlog_file}@{self.binlog_pos}")
            else:
                raise Exception("无法获取 binlog 位置，请确认源数据库已启用 binlog")
        except Error as e:
            raise Exception(f"获取 binlog 位置失败: {e}")

    def _listen_binlog(self):
        """使用 mysql-replication 流式监听 binlog 事件"""
        import random
        pymysql_settings = {
            "host": self.source_config['host'],
            "port": self.source_config['port'],
            "user": self.source_config['user'],
            "password": self.source_config['password'],
            "database": self.source_config.get('database', ''),
        }

        # 使用随机 server_id 避免多任务冲突（范围 10000-65000）
        server_id = random.randint(10000, 65000)
        logger.info(f"同步任务 {self.task_id} 使用 server_id={server_id}")

        stream = BinLogStreamReader(
            connection_settings=pymysql_settings,
            server_id=server_id,
            log_file=self.binlog_file,
            log_pos=self.binlog_pos,
            only_events=[WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent, QueryEvent, RotateEvent],
            blocking=True,
            resume_stream=True,
        )

        logger.info(f"同步任务 {self.task_id} 开始监听 binlog: {self.binlog_file}@{self.binlog_pos}")
        self._update_task_status(SyncStatus.RUNNING)
        self._write_run_log("INFO", f"开始监听 binlog: {self.binlog_file}@{self.binlog_pos}")

        try:
            for binlog_event in stream:
                if not self.running:
                    break

                if isinstance(binlog_event, RotateEvent):
                    self.binlog_file = binlog_event.next_binlog
                    self.binlog_pos = 4
                    logger.info(f"同步任务 {self.task_id} binlog 轮转: {self.binlog_file}")
                    self._write_run_log("INFO", f"binlog 轮转: {self.binlog_file}")
                    continue

                if isinstance(binlog_event, QueryEvent):
                    # DDL 语句（CREATE TABLE, ALTER TABLE 等）
                    query = binlog_event.query.strip()
                    if query.upper().startswith(('CREATE', 'ALTER', 'DROP', 'RENAME', 'TRUNCATE')):
                        self._replicate_ddl(query)
                        self._write_run_log("INFO", f"DDL 同步: {query[:200]}", detail=query)
                    continue

                # DML 行事件
                self._process_row_event(binlog_event)
                self._sync_count += 1

                # 每100条事件更新一次状态并记录日志
                if self._sync_count % 100 == 0:
                    self._update_task_status(SyncStatus.RUNNING)
                    self.binlog_pos = binlog_event.packet.log_pos
                    self._write_run_log("INFO", f"已同步 {self._sync_count} 条事件, 当前位置: {self.binlog_file}@{self.binlog_pos}")
                    logger.info(f"同步任务 {self.task_id} 已同步 {self._sync_count} 条事件")

        except Exception as e:
            logger.error(f"同步任务 {self.task_id} binlog 监听异常: {e}")
            self._write_run_log("ERROR", f"binlog 监听异常: {str(e)}", detail=str(e))
            raise
        finally:
            stream.close()

    def _process_row_event(self, event):
        """处理行级 binlog 事件并同步到目标数据库"""
        table = event.table
        database = event.schema

        if isinstance(event, WriteRowsEvent):
            for row in event.rows:
                # pymysqlreplication 返回的 row 结构: {"values": {col: val, ...}, "none_sources": {...}}
                values = row.get("values", row)
                # 如果列名无效（整数键或 UNKNOWN_COL），从源数据库查询真实列名
                if not self._is_valid_column_dict(values):
                    columns = self._get_column_names(database, table)
                    if columns:
                        values = self._map_values_to_columns(values, columns)
                        logger.debug(f"已将 {database}.{table} 的列名映射为: {list(values.keys())}")
                self._replicate_insert(database, table, values)
            if self._sync_count == 0:
                self._write_run_log("INFO", f"收到首个 INSERT 事件: {database}.{table}")

        elif isinstance(event, UpdateRowsEvent):
            for row in event.rows:
                # UpdateRowsEvent 返回: {"before_values": {...}, "after_values": {...}}
                before = row.get("before_values", {})
                after = row.get("after_values", {})
                # 如果列名无效，从源数据库查询真实列名
                if not self._is_valid_column_dict(after):
                    columns = self._get_column_names(database, table)
                    if columns:
                        before = self._map_values_to_columns(before, columns)
                        after = self._map_values_to_columns(after, columns)
                self._replicate_update(database, table, before, after)
            if self._sync_count == 0:
                self._write_run_log("INFO", f"收到首个 UPDATE 事件: {database}.{table}")

        elif isinstance(event, DeleteRowsEvent):
            for row in event.rows:
                # DeleteRowsEvent 返回: {"values": {col: val, ...}, "none_sources": {...}}
                values = row.get("values", row)
                # 如果列名无效，从源数据库查询真实列名
                if not self._is_valid_column_dict(values):
                    columns = self._get_column_names(database, table)
                    if columns:
                        values = self._map_values_to_columns(values, columns)
                self._replicate_delete(database, table, values)
            if self._sync_count == 0:
                self._write_run_log("INFO", f"收到首个 DELETE 事件: {database}.{table}")

    def _replicate_insert(self, database: str, table: str, row: dict):
        """将 INSERT 事件同步到目标数据库"""
        columns = ", ".join(f"`{k}`" for k in row.keys())
        placeholders = ", ".join(["%s"] * len(row))
        sql = f"INSERT INTO `{database}`.`{table}` ({columns}) VALUES ({placeholders})"
        self._execute_on_target(sql, list(row.values()))

    def _replicate_update(self, database: str, table: str, before: dict, after: dict):
        """将 UPDATE 事件同步到目标数据库"""
        set_clause = ", ".join(f"`{k}` = %s" for k in after.keys())
        where_clause = " AND ".join(f"`{k}` = %s" for k in before.keys())
        sql = f"UPDATE `{database}`.`{table}` SET {set_clause} WHERE {where_clause}"
        params = list(after.values()) + list(before.values())
        self._execute_on_target(sql, params)

    def _replicate_delete(self, database: str, table: str, row: dict):
        """将 DELETE 事件同步到目标数据库"""
        where_clause = " AND ".join(f"`{k}` = %s" for k in row.keys())
        sql = f"DELETE FROM `{database}`.`{table}` WHERE {where_clause}"
        self._execute_on_target(sql, list(row.values()))

    def _replicate_ddl(self, query: str):
        """将 DDL 语句同步到目标数据库"""
        try:
            cursor = self.target_conn.cursor()
            cursor.execute(query)
            self.target_conn.commit()
            cursor.close()
            logger.info(f"同步任务 {self.task_id} DDL 同步成功: {query[:100]}")
        except Exception as e:
            logger.warning(f"同步任务 {self.task_id} DDL 同步失败（跳过）: {e}")
            self._write_run_log("WARNING", f"DDL 同步失败（已跳过）: {str(e)[:200]}", detail=query)

    def _execute_on_target(self, sql: str, params: list):
        """在目标数据库执行 SQL"""
        try:
            if not self.target_conn or not self.target_conn.is_connected():
                self._connect_target()
            cursor = self.target_conn.cursor()
            cursor.execute(sql, params)
            self.target_conn.commit()
            cursor.close()
        except Exception as e:
            error_detail = f"SQL: {sql[:200]}... | 参数: {str(params)[:200]} | 错误: {e}"
            logger.error(f"同步任务 {self.task_id} 目标数据库执行失败: {error_detail}")
            self._write_run_log("ERROR", f"目标数据库执行失败: {str(e)[:200]}", detail=error_detail)
            # 尝试重连后重试一次
            try:
                self._connect_target()
                cursor = self.target_conn.cursor()
                cursor.execute(sql, params)
                self.target_conn.commit()
                cursor.close()
            except Exception as retry_err:
                retry_detail = f"SQL: {sql[:200]}... | 重试错误: {retry_err}"
                logger.error(f"同步任务 {self.task_id} 重试执行失败: {retry_detail}")
                self._write_run_log("ERROR", f"重试执行失败: {str(retry_err)[:200]}", detail=retry_detail)

    def _update_task_status(self, status: SyncStatus, error_message: str = None):
        """更新任务状态"""
        try:
            db = SessionLocal()
            task = db.query(SyncTask).filter(SyncTask.id == self.task_id).first()
            if task:
                task.status = status
                task.last_sync_time = now_beijing()
                task.sync_delay = self._sync_count
                if self.binlog_file:
                    task.binlog_file = self.binlog_file
                if self.binlog_pos:
                    task.binlog_position = str(self.binlog_pos)
                if error_message:
                    task.error_message = error_message
                db.commit()
            db.close()
        except Exception as e:
            logger.error(f"更新任务状态失败: {e}")

    def _write_run_log(self, level: str, message: str, detail: str = None):
        """写入运行日志"""
        try:
            db = SessionLocal()
            task = db.query(SyncTask).filter(SyncTask.id == self.task_id).first()
            task_name = task.name if task else f"Task#{self.task_id}"
            log = RunLog(
                task_type="sync",
                task_id=self.task_id,
                task_name=task_name,
                level=level,
                message=message,
                detail=detail
            )
            db.add(log)
            db.commit()
            db.close()
        except Exception as e:
            logger.error(f"写入运行日志失败: {e}")


class SyncService:
    """同步服务管理"""
    
    def __init__(self):
        self.sync_tasks: Dict[int, MySQLBinlogSync] = {}
        self.running = False
    
    async def start(self):
        """启动同步服务"""
        self.running = True
        logger.info("同步服务已启动")
        
        # 加载并启动所有活跃的同步任务
        await self._load_active_tasks()
    
    async def stop(self):
        """停止同步服务"""
        self.running = False
        
        # 停止所有同步任务
        for task_id, sync in self.sync_tasks.items():
            sync.stop()
        
        self.sync_tasks.clear()
        logger.info("同步服务已停止")
    
    async def _load_active_tasks(self):
        """加载活跃的同步任务"""
        db = SessionLocal()
        try:
            tasks = db.query(SyncTask).filter(
                SyncTask.status == SyncStatus.RUNNING
            ).all()
            
            logger.info(f"发现 {len(tasks)} 个活跃的同步任务")
            
            for task in tasks:
                try:
                    await self.start_task(task.id)
                except Exception as e:
                    logger.error(f"加载同步任务 {task.id} ({task.name}) 失败: {e}")
                    # 继续加载其他任务，不因单个失败而中断
                    continue
            
        except Exception as e:
            logger.error(f"查询活跃同步任务失败: {e}")
        finally:
            db.close()
    
    async def start_task(self, task_id: int):
        """启动同步任务"""
        # 检查是否真正在运行（不只是字典中存在）
        if task_id in self.sync_tasks and self.sync_tasks[task_id].running:
            logger.warning(f"同步任务 {task_id} 已在运行中")
            return
        
        try:
            db = SessionLocal()
            task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
            if not task:
                raise Exception(f"同步任务 {task_id} 不存在")
            
            # 获取源数据库和目标数据库配置
            source_db = db.query(Database).filter(Database.id == task.source_db_id).first()
            target_db = db.query(Database).filter(Database.id == task.target_db_id).first()
            
            if not source_db or not target_db:
                raise Exception("源数据库或目标数据库不存在")
            
            # 创建同步实例
            source_config = {
                'host': source_db.host,
                'port': source_db.port,
                'user': source_db.username,
                'password': decrypt(source_db.password),
                'database': source_db.database_name
            }

            target_config = {
                'host': target_db.host,
                'port': target_db.port,
                'user': target_db.username,
                'password': decrypt(target_db.password),
                'database': target_db.database_name
            }
            
            sync = MySQLBinlogSync(task_id, source_config, target_config)
            self.sync_tasks[task_id] = sync
            
            # 启动同步
            sync.start()
            
            # 更新任务状态
            task.status = SyncStatus.RUNNING
            db.commit()
            db.close()
            
            logger.info(f"同步任务 {task_id} 启动成功")
            
        except Exception as e:
            logger.error(f"启动同步任务 {task_id} 失败: {e}")
            # 从运行列表中移除
            if task_id in self.sync_tasks:
                del self.sync_tasks[task_id]
            # 更新任务状态为失败
            self._update_task_status(task_id, SyncStatus.FAILED, str(e))
            self._write_run_log_to_db(task_id, "ERROR", f"启动同步任务失败: {str(e)}", "sync")
            raise
    
    async def stop_task(self, task_id: int):
        """停止同步任务"""
        if task_id not in self.sync_tasks:
            logger.warning(f"同步任务 {task_id} 未在运行")
            return
        
        try:
            sync = self.sync_tasks[task_id]
            sync.stop()
            del self.sync_tasks[task_id]
            
            # 更新任务状态
            self._update_task_status(task_id, SyncStatus.STOPPED)
            
            logger.info(f"同步任务 {task_id} 停止成功")
            
        except Exception as e:
            logger.error(f"停止同步任务 {task_id} 失败: {e}")
            self._write_run_log_to_db(task_id, "ERROR", f"停止同步任务失败: {str(e)}", "sync")
            raise
    
    def _update_task_status(self, task_id: int, status: SyncStatus, error_message: str = None):
        """更新任务状态"""
        try:
            db = SessionLocal()
            task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
            if task:
                task.status = status
                if error_message:
                    task.error_message = error_message
                db.commit()
            db.close()
        except Exception as e:
            logger.error(f"更新任务状态失败: {e}")

    def _write_run_log_to_db(self, task_id: int, level: str, message: str, task_type: str = "sync"):
        """写入运行日志到数据库"""
        try:
            db = SessionLocal()
            task = db.query(SyncTask).filter(SyncTask.id == task_id).first()
            task_name = task.name if task else f"Task#{task_id}"
            log = RunLog(
                task_type=task_type,
                task_id=task_id,
                task_name=task_name,
                level=level,
                message=message
            )
            db.add(log)
            db.commit()
            db.close()
        except Exception as e:
            logger.error(f"写入运行日志失败: {e}")
    
    def get_task_status(self, task_id: int) -> Optional[Dict]:
        """获取任务状态"""
        if task_id in self.sync_tasks:
            sync = self.sync_tasks[task_id]
            return {
                "task_id": task_id,
                "running": sync.running,
                "status": "running" if sync.running else "stopped"
            }
        return None