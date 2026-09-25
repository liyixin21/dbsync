/**
 * HTTP 客户端。
 *
 * 单一入口，统一处理：
 * - Bearer 令牌注入
 * - 401 自动登出
 * - 后端 {"error": {...}} 外壳的解包
 * - 网络错误与超时的可读提示
 */

import type {
  AppLogs,
  BackupHistoryItem,
  BackupPlan,
  BackupPlanPayload,
  BackupStatistics,
  ConnectionTestResult,
  DatabaseConfig,
  DatabasePayload,
  FullCopyResult,
  LoginLog,
  LoginResponse,
  LogStatistics,
  OpenListConfig,
  OpenListConfigPayload,
  OpenListTestResult,
  OperationLog,
  Page,
  RunLog,
  SyncError,
  SyncTableState,
  SyncTask,
  SyncTaskPayload,
  SystemStatus,
  ThemeConfig,
  UserInfo,
} from './types'

const TOKEN_KEY = 'dbsync_token'
const USER_KEY = 'dbsync_user'

export class ApiError extends Error {
  code: string
  status: number
  detail?: unknown
  /**
   * 该错误是否已由全局机制呈现过。
   *
   * 会话过期就是这种情况：右上角已弹出常驻横幅，
   * 调用方再弹一次 toast 就是同一条信息说两遍。
   */
  silent: boolean

  constructor(
    message: string,
    code: string,
    status: number,
    detail?: unknown,
    silent = false,
  ) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.detail = detail
    this.silent = silent
  }
}

// ---------------------------------------------------------------- 令牌

export const auth = {
  get token(): string | null {
    return localStorage.getItem(TOKEN_KEY)
  },
  get user(): string | null {
    return localStorage.getItem(USER_KEY)
  },
  save(token: string, username: string): void {
    localStorage.setItem(TOKEN_KEY, token)
    localStorage.setItem(USER_KEY, username)
  },
  clear(): void {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  },
}

/** 401 时的回调，由应用层注入以避免循环依赖。 */
let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn
}

// ---------------------------------------------------------------- 核心请求

interface RequestOptions {
  method?: string
  body?: unknown
  query?: Record<string, string | number | boolean | undefined | null>
  /** 覆盖默认超时（毫秒） */
  timeout?: number
  /** 返回原始 Response，用于下载 */
  raw?: boolean
  /**
   * 该请求的 401 是否表示「会话已过期」。
   *
   * 登录请求本身返回 401 只表示账号密码错误，不能据此置位会话过期标志，
   * 否则用户输错密码会看到「登录已过期」，且过期标志被污染。
   */
  authProbe?: boolean
}

const DEFAULT_TIMEOUT = 30000
/** 备份/恢复/全量复制这类长任务单独放宽 */
const LONG_TIMEOUT = 3600000

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = `/api${path}`
  if (!query) return url
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    params.append(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const {
    method = 'GET', body, query, timeout = DEFAULT_TIMEOUT, raw = false,
    authProbe = false,
  } = options

  const headers: Record<string, string> = {}
  const token = auth.token
  if (token) headers['Authorization'] = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeout)

  let response: Response
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })
  } catch (err) {
    window.clearTimeout(timer)
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError('请求超时，请检查网络或服务状态', 'TIMEOUT', 0)
    }
    throw new ApiError('无法连接到服务器，请确认服务正在运行', 'NETWORK_ERROR', 0)
  } finally {
    window.clearTimeout(timer)
  }

  if (response.status === 401) {
    if (authProbe) {
      // 登录接口的 401 表示凭据错误，不是会话过期
      auth.clear()
      const payload = await response.json().catch(() => null)
      const msg = payload?.error?.message || '用户名或密码错误'
      throw new ApiError(msg, 'INVALID_CREDENTIALS', 401)
    }
    auth.clear()
    if (onUnauthorized) onUnauthorized()
    // silent=true：过期状态由右上角常驻横幅统一呈现，调用方不再重复提示
    throw new ApiError('登录已过期，请重新登录', 'UNAUTHORIZED', 401, undefined, true)
  }

  if (raw) {
    if (!response.ok) {
      throw new ApiError(`请求失败 (${response.status})`, 'HTTP_ERROR', response.status)
    }
    return response as unknown as T
  }

  const contentType = response.headers.get('content-type') || ''
  let payload: unknown = null
  if (contentType.includes('application/json')) {
    payload = await response.json().catch(() => null)
  } else {
    payload = await response.text().catch(() => null)
  }

  if (!response.ok) {
    const envelope = payload as { error?: { code: string; message: string; detail?: unknown } } | null
    if (envelope && typeof envelope === 'object' && envelope.error) {
      throw new ApiError(
        envelope.error.message || '请求失败',
        envelope.error.code || 'ERROR',
        response.status,
        envelope.error.detail,
      )
    }
    const text = typeof payload === 'string' ? payload : ''
    throw new ApiError(text || `请求失败 (${response.status})`, 'ERROR', response.status)
  }

  return payload as T
}

// ---------------------------------------------------------------- 业务端点

export const api = {
  // ---------------- 认证
  login(username: string, password: string) {
    return request<LoginResponse>('/auth/login', {
      method: 'POST',
      body: { username, password },
      authProbe: true,
    })
  },
  me() {
    return request<UserInfo>('/auth/me')
  },
  listUsers() {
    return request<UserInfo[]>('/auth/users')
  },
  createUser(username: string, password: string) {
    return request<UserInfo>('/auth/users', { method: 'POST', body: { username, password } })
  },
  changeUsername(password: string, newUsername: string) {
    return request<{ message: string; access_token: string; username: string }>(
      '/auth/change-username',
      { method: 'PUT', body: { password, new_username: newUsername } },
    )
  },
  changePassword(oldPassword: string, newPassword: string) {
    return request<{ message: string; access_token: string; username: string }>(
      '/auth/change-password',
      { method: 'PUT', body: { old_password: oldPassword, new_password: newPassword } },
    )
  },

  // ---------------- 数据库
  listDatabases() {
    return request<DatabaseConfig[]>('/databases/')
  },
  getDatabase(id: number) {
    return request<DatabaseConfig>(`/databases/${id}`)
  },
  createDatabase(payload: DatabasePayload) {
    return request<DatabaseConfig>('/databases/', { method: 'POST', body: payload })
  },
  updateDatabase(id: number, payload: Partial<DatabasePayload>) {
    return request<DatabaseConfig>(`/databases/${id}`, { method: 'PUT', body: payload })
  },
  deleteDatabase(id: number) {
    return request<{ message: string }>(`/databases/${id}`, { method: 'DELETE' })
  },
  testConnection(payload: DatabasePayload & { database_id?: number }) {
    return request<ConnectionTestResult>('/databases/test-connection', {
      method: 'POST',
      body: payload,
      timeout: 20000,
    })
  },

  // ---------------- 同步任务
  listSyncTasks() {
    return request<SyncTask[]>('/sync-tasks/')
  },
  getSyncTask(id: number) {
    return request<SyncTask>(`/sync-tasks/${id}`)
  },
  createSyncTask(payload: SyncTaskPayload) {
    return request<SyncTask>('/sync-tasks/', { method: 'POST', body: payload })
  },
  updateSyncTask(id: number, payload: Partial<SyncTaskPayload>) {
    return request<SyncTask>(`/sync-tasks/${id}`, { method: 'PUT', body: payload })
  },
  deleteSyncTask(id: number) {
    return request<{ message: string }>(`/sync-tasks/${id}`, { method: 'DELETE' })
  },
  startSyncTask(id: number) {
    return request<{ message: string }>(`/sync-tasks/${id}/start`, {
      method: 'POST',
      timeout: 60000,
    })
  },
  stopSyncTask(id: number) {
    return request<{ message: string }>(`/sync-tasks/${id}/stop`, {
      method: 'POST',
      timeout: 60000,
    })
  },
  taskTables(id: number) {
    return request<SyncTableState[]>(`/sync-tasks/${id}/tables`)
  },
  taskErrors(id: number, skip = 0, limit = 50, unresolvedOnly = true) {
    return request<Page<SyncError>>(`/sync-tasks/${id}/errors`, {
      query: { skip, limit, unresolved_only: unresolvedOnly },
    })
  },
  clearTaskErrors(id: number) {
    return request<{ message: string }>(`/sync-tasks/${id}/errors`, { method: 'DELETE' })
  },
  retryTaskError(taskId: number, errorId: number) {
    return request<{ message: string }>(`/sync-tasks/${taskId}/errors/${errorId}/retry`, {
      method: 'POST',
    })
  },
  fullCopy(id: number) {
    return request<FullCopyResult>(`/sync-tasks/${id}/full-copy`, {
      method: 'POST',
      timeout: LONG_TIMEOUT,
    })
  },

  // ---------------- 备份计划
  listBackupPlans() {
    return request<BackupPlan[]>('/backup-plans/')
  },
  createBackupPlan(payload: BackupPlanPayload) {
    return request<BackupPlan>('/backup-plans/', { method: 'POST', body: payload })
  },
  updateBackupPlan(id: number, payload: Partial<BackupPlanPayload>) {
    return request<BackupPlan>(`/backup-plans/${id}`, { method: 'PUT', body: payload })
  },
  deleteBackupPlan(id: number, deleteFiles = true) {
    return request<{ message: string; deleted_files: number | null }>(
      `/backup-plans/${id}`,
      { method: 'DELETE', query: { delete_files: deleteFiles } },
    )
  },
  executeBackupPlan(id: number) {
    return request<{ message: string; plan_id: number; history_id: number | null }>(
      `/backup-plans/${id}/execute`,
      { method: 'POST', timeout: LONG_TIMEOUT },
    )
  },

  // ---------------- 备份历史
  listBackupHistory(skip = 0, limit = 20) {
    return request<Page<BackupHistoryItem>>('/backup-history/', { query: { skip, limit } })
  },
  backupStatistics() {
    return request<BackupStatistics>('/backup-history/statistics')
  },
  deleteBackupHistory(id: number, deleteFiles = true) {
    return request<{ message: string }>(`/backup-history/${id}`, {
      method: 'DELETE',
      query: { delete_files: deleteFiles },
    })
  },
  batchDeleteBackupHistory(ids: number[], deleteFiles = true) {
    return request<{ message: string; affected: number }>('/backup-history/batch', {
      method: 'POST',
      body: { ids, delete_files: deleteFiles },
    })
  },
  clearBackupHistory(deleteFiles = true) {
    return request<{ message: string }>('/backup-history/clear', {
      method: 'DELETE',
      query: { delete_files: deleteFiles },
    })
  },
  restoreBackup(id: number, targetDatabaseId?: number) {
    return request<{ message: string }>(`/backup-history/${id}/restore`, {
      method: 'POST',
      body: { target_database_id: targetDatabaseId ?? null },
      timeout: LONG_TIMEOUT,
    })
  },
  /** 下载备份文件。返回 Blob，由调用方触发保存。 */
  async downloadBackup(id: number): Promise<Blob> {
    const response = await request<Response>(`/backup-history/${id}/download`, { raw: true })
    return response.blob()
  },

  // ---------------- 日志
  operationLogs(params: { skip?: number; limit?: number; search?: string } = {}) {
    return request<Page<OperationLog>>('/logs/operations', { query: params })
  },
  loginLogs(params: { skip?: number; limit?: number; search?: string } = {}) {
    return request<Page<LoginLog>>('/logs/logins', { query: params })
  },
  runLogs(params: { skip?: number; limit?: number; search?: string } = {}) {
    return request<Page<RunLog>>('/logs/runs', { query: params })
  },
  logStatistics() {
    return request<LogStatistics>('/logs/statistics')
  },
  deleteLog(type: 'operations' | 'logins' | 'runs', id: number) {
    return request<{ message: string }>(`/logs/${type}/${id}`, { method: 'DELETE' })
  },
  clearLogs(type: 'operations' | 'logins' | 'runs') {
    return request<{ message: string }>(`/logs/${type}-clear`, { method: 'DELETE' })
  },

  // ---------------- 系统
  systemStatus() {
    return request<SystemStatus>('/system/status')
  },
  getTheme() {
    return request<ThemeConfig>('/system/theme')
  },
  updateTheme(payload: Partial<ThemeConfig>) {
    return request<{ message: string; theme: ThemeConfig }>('/system/theme', {
      method: 'PUT',
      body: payload,
    })
  },
  getConfig(key: string) {
    return request<{ key: string; value: string | null; description: string | null }>(
      `/system/configs/${key}`,
    )
  },
  updateConfig(key: string, value: string) {
    return request<{ key: string; value: string | null }>(`/system/configs/${key}`, {
      method: 'PUT',
      body: { value },
    })
  },
  appLogs(lines = 200, level?: string, keyword?: string) {
    return request<AppLogs>('/system/logs', { query: { lines, level, keyword } })
  },

  // ---------------- OpenList 上传
  getOpenListConfig() {
    return request<OpenListConfig>('/system/openlist')
  },
  saveOpenListConfig(payload: OpenListConfigPayload) {
    return request<OpenListConfig>('/system/openlist', { method: 'PUT', body: payload })
  },
  testOpenList(payload: Partial<OpenListConfigPayload>) {
    return request<OpenListTestResult>('/system/openlist/test', {
      method: 'POST',
      body: payload,
      timeout: 30000,
    })
  },
  /** 手动上传某次备份到 OpenList */
  uploadBackup(id: number) {
    return request<{ message: string }>(`/backup-history/${id}/upload`, {
      method: 'POST',
      timeout: LONG_TIMEOUT,
    })
  },
}
