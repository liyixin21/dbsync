/**
 * 通用格式化工具。
 */

export function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value).getTime()
  if (Number.isNaN(date)) return '—'
  const diff = Date.now() - date
  if (diff < 0) return formatTime(value)
  const seconds = Math.floor(diff / 1000)
  if (seconds < 60) return `${seconds} 秒前`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  return formatTime(value)
}

export function formatSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let index = 0
  let value = bytes
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${index === 0 ? value : value.toFixed(value >= 100 ? 0 : 1)} ${units[index]}`
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  if (minutes < 60) return `${minutes}m ${rest}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

/** 把秒数转成「1天2小时」这类可读形式。 */
export function formatUptime(seconds: number): string {
  if (!seconds || seconds < 0) return '—'
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const parts: string[] = []
  if (days) parts.push(`${days} 天`)
  if (hours) parts.push(`${hours} 小时`)
  if (!days && minutes) parts.push(`${minutes} 分钟`)
  return parts.length ? parts.join(' ') : '刚刚'
}

const SYNC_STATUS_TEXT: Record<string, string> = {
  pending: '待启动',
  running: '运行中',
  paused: '已暂停',
  failed: '失败',
  stopped: '已停止',
}

const SYNC_HEALTH_TEXT: Record<string, string> = {
  healthy: '正常',
  degraded: '降级',
  stalled: '停滞',
  unknown: '未知',
}

const BACKUP_STATUS_TEXT: Record<string, string> = {
  pending: '等待中',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
}

const TABLE_STATE_TEXT: Record<string, string> = {
  active: '正常',
  schema_missing: '缺表',
  skipped: '已跳过',
  errored: '异常',
  no_primary_key: '无主键',
}

export const syncStatusText = (s: string): string => SYNC_STATUS_TEXT[s] ?? s
export const syncHealthText = (s: string): string => SYNC_HEALTH_TEXT[s] ?? s
export const backupStatusText = (s: string): string => BACKUP_STATUS_TEXT[s] ?? s
export const tableStateText = (s: string): string => TABLE_STATE_TEXT[s] ?? s

/** 徽章类别。与 Badge.vue 的 kind 属性取值一一对应。 */
export type BadgeKind = 'neutral' | 'primary' | 'success' | 'danger' | 'warning'

export function syncStatusKind(status: string): BadgeKind {
  switch (status) {
    case 'running':
      return 'success'
    case 'failed':
      return 'danger'
    case 'pending':
      return 'warning'
    case 'paused':
      return 'primary'
    default:
      return 'neutral'
  }
}

export function healthKind(health: string): BadgeKind {
  switch (health) {
    case 'healthy':
      return 'success'
    case 'degraded':
      return 'warning'
    case 'stalled':
      return 'danger'
    default:
      return 'neutral'
  }
}

export function backupStatusKind(status: string): BadgeKind {
  switch (status) {
    case 'completed':
      return 'success'
    case 'failed':
      return 'danger'
    case 'running':
      return 'primary'
    default:
      return 'neutral'
  }
}

export function tableStateKind(state: string): BadgeKind {
  switch (state) {
    case 'active':
      return 'success'
    case 'schema_missing':
      return 'warning'
    // 无主键不是错误：同步仍在工作，但 UPDATE/DELETE 靠全列匹配，
    // 遇到重复行会误删。用警示色提示，而不是「失败」红。
    case 'no_primary_key':
      return 'warning'
    case 'errored':
      return 'danger'
    default:
      return 'neutral'
  }
}

export function logLevelKind(level: string): BadgeKind {
  switch (level.toUpperCase()) {
    case 'ERROR':
    case 'CRITICAL':
      return 'danger'
    case 'WARNING':
      return 'warning'
    case 'DEBUG':
      return 'neutral'
    default:
      return 'primary'
  }
}

/** 布尔 → 启用/禁用徽章类别。 */
export function enabledKind(enabled: boolean): BadgeKind {
  return enabled ? 'success' : 'neutral'
}

/** 触发浏览器下载。 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
