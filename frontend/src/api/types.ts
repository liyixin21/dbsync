/**
 * 后端 API 的类型定义。与 app/api/*.py 中的 Pydantic 模型手工对齐。
 */

export interface PageMeta {
  total: number
  skip: number
  limit: number
}

export interface Page<T> {
  data: T[]
  page: PageMeta
}

export interface ApiErrorBody {
  error: {
    code: string
    message: string
    detail?: unknown
  }
}

// ---------------------------------------------------------------- 认证

export interface LoginResponse {
  access_token: string
  token_type: string
  username: string
  expires_in: number
}

export interface UserInfo {
  id: number
  username: string
  is_active: boolean
  created_at: string
  last_login: string | null
}

// ---------------------------------------------------------------- 数据库

export interface DatabaseConfig {
  id: number
  name: string
  host: string
  port: number
  username: string
  password_set: boolean
  database_name: string
  db_type: string
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface DatabasePayload {
  name: string
  host: string
  port: number
  username: string
  password?: string
  database_name: string
  is_active?: boolean
}

export interface ConnectionTestResult {
  success: boolean
  message: string
  server_version: string | null
}

// ---------------------------------------------------------------- 同步任务

export type SyncStatus = 'pending' | 'running' | 'paused' | 'failed' | 'stopped'
export type SyncHealth = 'healthy' | 'degraded' | 'stalled' | 'unknown'

export interface SyncTask {
  id: number
  name: string
  source_db_id: number
  target_db_id: number
  source_db_name: string | null
  target_db_name: string | null
  status: SyncStatus
  health: SyncHealth
  error_message: string | null
  auto_start: boolean

  gtid_set: string | null
  binlog_file: string | null
  binlog_position: number | null
  binlog_format: string | null
  binlog_row_image: string | null

  last_sync_time: string | null
  sync_delay: number
  applied_events: number
  dlq_count: number
  running: boolean

  created_at: string
  updated_at: string
}

export interface SyncTaskPayload {
  name: string
  source_db_id: number
  target_db_id: number
  auto_start?: boolean
}

export interface SyncTableState {
  schema_name: string
  table_name: string
  state: 'active' | 'schema_missing' | 'skipped' | 'errored' | 'no_primary_key'
  pk_columns: string[] | null
  unique_keys: string[][] | null
  last_binlog_file: string | null
  last_binlog_pos: number | null
  last_applied_at: string | null
  applied_events: number
  error_count: number
  last_error: string | null
}

export interface SyncError {
  id: number
  schema_name: string
  table_name: string
  event_type: string
  error_code: number | null
  error_message: string
  payload: string | null
  binlog_file: string | null
  binlog_pos: number | null
  retry_count: number
  resolved: boolean
  created_at: string
}

export interface FullCopyResult {
  message: string
  source: string
  target: string
  file_size: number
  duration: number
}

// ---------------------------------------------------------------- 备份

export interface BackupPlan {
  id: number
  name: string
  database_id: number
  database_name: string | null
  backup_type: string
  schedule_interval: number
  retention_count: number
  is_active: boolean
  running: boolean
  upload_enabled: boolean
  upload_dir: string | null
  last_run_at: string | null
  next_run_at: string | null
  created_at: string
  updated_at: string
}

export interface BackupPlanPayload {
  name: string
  database_id: number
  schedule_interval: number
  retention_count: number
  is_active: boolean
  upload_enabled?: boolean
  upload_dir?: string | null
}

export interface BackupHistoryItem {
  id: number
  backup_plan_id: number
  plan_name: string | null
  status: 'pending' | 'running' | 'completed' | 'failed'
  backup_type: string
  file_path: string | null
  file_size: number | null
  start_time: string | null
  end_time: string | null
  duration: number | null
  error_message: string | null
  trigger: string | null
  file_exists: boolean
  upload_status: 'skipped' | 'pending' | 'uploading' | 'success' | 'failed'
  upload_path: string | null
  upload_error: string | null
  uploaded_at: string | null
  created_at: string
}

export interface BackupStatistics {
  total_count: number
  status_stats: Record<string, number>
  recent_count: number
  total_size: number
  disk_file_count: number
  orphan_file_count: number
}

// ---------------------------------------------------------------- 日志

export interface OperationLog {
  id: number
  user_id: number | null
  username: string | null
  action: string
  resource_type: string | null
  resource_id: number | null
  resource_name: string | null
  detail: string | null
  ip_address: string | null
  created_at: string
}

export interface LoginLog {
  id: number
  username: string
  ip_address: string | null
  user_agent: string | null
  success: boolean
  failure_reason: string | null
  created_at: string
}

export interface RunLog {
  id: number
  task_type: string
  task_id: number
  task_name: string | null
  level: string
  message: string
  detail: string | null
  created_at: string
}

export interface LogStatistics {
  operations: { total: number; today: number }
  logins: { total: number; failed: number; today: number }
  runs: { total: number; errors: number }
}

// ---------------------------------------------------------------- 系统

export interface SystemStatus {
  app_name: string
  app_version: string
  uptime_seconds: number
  system_uptime_seconds: number
  database_count: number
  sync_tasks_count: number
  sync_running_count: number
  backup_plans_count: number
  last_backup_time: string | null
  disk_usage: { total?: number; used?: number; free?: number; percent?: number }
  backup_dir: string
  mysql_tools: Record<string, string | null>
}

export interface ThemeConfig {
  primary_color: string
  dark_mode: boolean
}

export interface AppLogs {
  logs: string[]
  total_lines: number
}

// ---------------------------------------------------------------- OpenList

export interface OpenListConfig {
  enabled: boolean
  base_url: string
  username: string
  remote_dir: string
  verify_ssl: boolean
  password_set: boolean
}

export interface OpenListConfigPayload {
  enabled: boolean
  base_url: string
  username: string
  /** 留空表示不修改已保存的密码 */
  password?: string | null
  remote_dir: string
  verify_ssl: boolean
}

export interface OpenListTestResult {
  success: boolean
  message: string
  directory: string | null
}
