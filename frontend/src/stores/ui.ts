/**
 * 全局 UI 状态：主题、提示条、会话状态、全屏加载。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api/client'

export type ToastKind = 'success' | 'error' | 'info' | 'warning'
export type ThemeMode = 'light' | 'dark' | 'system'

export interface Toast {
  id: number
  kind: ToastKind
  message: string
}

const DARK_KEY = 'dbsync_dark'
const PRIMARY_KEY = 'dbsync_primary'
const DEFAULT_PRIMARY = '#2f6feb'

let toastSeq = 0

export const useUiStore = defineStore('ui', () => {
  const toasts = ref<Toast[]>([])
  const loading = ref('')
  const dark = ref(false)
  const primary = ref(DEFAULT_PRIMARY)

  /** 主题模式。'system' 表示跟随操作系统，此时不写入 localStorage。 */
  const mode = ref<ThemeMode>('system')

  /**
   * 会话已过期标志。
   *
   * 持久化到 sessionStorage：整页刷新会重建 Pinia store，
   * 若只存内存，用户刷新后过期状态就丢失、又能进入受保护页面，
   * 直到下一次 API 调用再次 401 —— 表现为反复弹窗与页面闪跳。
   *
   * 用 sessionStorage 而非 localStorage：标签页关闭即清除，
   * 新开的标签页不应继承「已过期」状态。
   */
  const SESSION_KEY = 'dbsync_session_expired'

  const sessionExpired = ref(sessionStorage.getItem(SESSION_KEY) === '1')

  /**
   * 过期横幅当前是否可见。
   *
   * 由横幅组件控制生命周期；登录页据此决定是否显示行内说明，
   * 保证两者不会同时出现（同一条信息不说两遍）。
   */
  const sessionBannerVisible = ref(false)

  const systemPrefersDark = (): boolean =>
    window.matchMedia('(prefers-color-scheme: dark)').matches

  // ---------------------------------------------------------------- 主题

  /** 写入 DOM。所有主题变更的唯一出口，不触碰存储。 */
  function renderTheme(): void {
    document.documentElement.setAttribute(
      'data-theme',
      dark.value ? 'dark' : 'light',
    )
    document.documentElement.style.setProperty('--primary', primary.value)
  }

  /**
   * 应用主题。
   *
   * persist 为真才会写入 localStorage——这是关键：
   * 若「跟随系统」也写存储，就变成了一次性快照，系统偏好此后不再生效。
   */
  function applyTheme(persist = false): void {
    renderTheme()
    if (persist) {
      if (mode.value === 'system') {
        localStorage.removeItem(DARK_KEY)
      } else {
        localStorage.setItem(DARK_KEY, dark.value ? '1' : '0')
      }
      localStorage.setItem(PRIMARY_KEY, primary.value)
    }
  }

  /**
   * 初始化主题。
   *
   * 三态语义：
   *   localStorage 无值  → 跟随系统（mode='system'）
   *   '1'                → 明确深色
   *   '0'                → 明确浅色
   *
   * 旧实现无条件读 localStorage，首访时 null === '1' 恒为 false，
   * 于是把深色系统误判为浅色，并把结果写回存储、永久覆盖系统偏好。
   */
  function initTheme(): void {
    const saved = localStorage.getItem(DARK_KEY)
    if (saved === null) {
      mode.value = 'system'
      dark.value = systemPrefersDark()
    } else {
      mode.value = saved === '1' ? 'dark' : 'light'
      dark.value = saved === '1'
    }

    const savedPrimary = localStorage.getItem(PRIMARY_KEY)
    if (savedPrimary) primary.value = savedPrimary

    renderTheme()

    // 跟随系统期间响应操作系统主题变化
    window
      .matchMedia('(prefers-color-scheme: dark)')
      .addEventListener('change', (event) => {
        if (mode.value !== 'system') return
        dark.value = event.matches
        renderTheme()
      })
  }

  /** 明确设置主题模式。用户主动操作时调用，会持久化。 */
  function setMode(next: ThemeMode): void {
    mode.value = next
    dark.value = next === 'system' ? systemPrefersDark() : next === 'dark'
    applyTheme(true)
    void api.updateTheme({ dark_mode: dark.value }).catch(() => {
      /* 服务端持久化失败不影响本地体验 */
    })
  }

  /** 在浅色/深色之间切换（跳过跟随系统）。 */
  function toggleDark(): void {
    setMode(dark.value ? 'light' : 'dark')
  }

  function setPrimary(color: string): void {
    primary.value = color
    applyTheme(true)
    void api.updateTheme({ primary_color: color }).catch(() => {})
  }

  /**
   * 从服务端加载主题偏好。
   *
   * 关键：当本地没有任何明确选择时（首访），保持「跟随系统」，
   * 不能用服务端默认值覆盖。
   *
   * 旧实现在这里无条件取 theme.dark_mode（后端默认 false），
   * 把首访用户钉死成浅色——即使操作系统是深色，也看不到深色界面，
   * 而且「跟随系统」从此形同虚设。
   *
   * 服务端配置只在用户已经明确选择过模式时才作为补充参考（主题色）。
   */
  async function loadTheme(): Promise<void> {
    try {
      const theme = await api.getTheme()

      // 主题色：本地已有时以本地为准
      if (theme.primary_color && !localStorage.getItem(PRIMARY_KEY)) {
        primary.value = theme.primary_color
      }

      // 深浅色：只在本地留下了明确偏好时才采用服务端值；
      // 否则维持 initTheme() 定下的「跟随系统」语义。
      const savedMode = localStorage.getItem(DARK_KEY)
      if (savedMode !== null) {
        mode.value = savedMode === '1' ? 'dark' : 'light'
        dark.value = savedMode === '1'
      }

      renderTheme()
    } catch {
      /* 保留本地状态 */
    }
  }

  // ---------------------------------------------------------------- 会话

  function markSessionExpired(): void {
    sessionExpired.value = true
    try {
      sessionStorage.setItem(SESSION_KEY, '1')
    } catch {
      /* 隐私模式下不可用，退化为仅内存标记 */
    }
  }

  function clearSessionExpired(): void {
    sessionExpired.value = false
    try {
      sessionStorage.removeItem(SESSION_KEY)
    } catch {
      /* 同上 */
    }
  }

  // ---------------------------------------------------------------- 提示条

  /**
   * 弹出提示。
   *
   * 默认时长偏长（7 秒）：原先 4.2 秒对中文长句来说经常来不及读完，
   * 尤其错误信息通常较长且需要理解。
   */
  function toast(message: string, kind: ToastKind = 'info', timeout = 7000): void {
    const id = ++toastSeq
    toasts.value = [...toasts.value, { id, kind, message }]
    if (timeout > 0) {
      window.setTimeout(() => dismissToast(id), timeout)
    }
  }

  function dismissToast(id: number): void {
    toasts.value = toasts.value.filter((t) => t.id !== id)
  }

  const success = (m: string) => toast(m, 'success', 4000)
  const error = (m: string) => toast(m, 'error', 10000)
  const info = (m: string) => toast(m, 'info', 6000)
  const warn = (m: string) => toast(m, 'warning', 8000)

  /**
   * 把任意异常转成可读信息并弹出。
   *
   * 带 silent 标志的错误（如会话过期）已由全局机制呈现过，
   * 这里直接跳过，避免同一条信息重复出现。
   */
  function fail(err: unknown, fallback = '操作失败'): void {
    if (err && typeof err === 'object' && (err as { silent?: boolean }).silent) return
    const message = err instanceof Error ? err.message : fallback
    error(message)
  }

  // ---------------------------------------------------------------- 全屏加载

  function setLoading(message = ''): void {
    loading.value = message
  }
  function clearLoading(): void {
    loading.value = ''
  }

  return {
    toasts,
    loading,
    dark,
    primary,
    mode,
    sessionExpired,
    sessionBannerVisible,
    initTheme,
    loadTheme,
    setMode,
    toggleDark,
    setPrimary,
    markSessionExpired,
    clearSessionExpired,
    toast,
    dismissToast,
    success,
    error,
    info,
    warn,
    fail,
    setLoading,
    clearLoading,
  }
})
