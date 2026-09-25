<script setup lang="ts">
import { onMounted, ref } from 'vue'
import Icon from '@/components/Icon.vue'
import { api } from '@/api/client'
import type { AppLogs } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import type { ThemeMode } from '@/stores/ui'

const ui = useUiStore()
const auth = useAuthStore()

const THEME_OPTIONS: Array<{ value: ThemeMode; label: string; icon: string }> = [
  { value: 'light', label: '浅色', icon: 'sun' },
  { value: 'dark', label: '深色', icon: 'moon' },
  { value: 'system', label: '跟随系统', icon: 'dashboard' },
]

// 用户名
const usernamePassword = ref('')
const newUsername = ref('')
const savingUsername = ref(false)

// 密码
const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const savingPassword = ref(false)

// 外观
const primaryLocal = ref('#2f6feb')

// 系统
const backupDir = ref('')
const savingDir = ref(false)

// OpenList 上传配置
const openlist = ref({
  enabled: false,
  base_url: '',
  username: '',
  password: '',
  remote_dir: '/',
  verify_ssl: true,
  password_set: false,
})
const savingOpenlist = ref(false)
const testingOpenlist = ref(false)
const openlistTestResult = ref<{ success: boolean; message: string } | null>(null)

// 应用日志
const appLogs = ref<AppLogs | null>(null)
const logLines = ref(200)
const logLevel = ref('')
const loadingLogs = ref(false)

onMounted(async () => {
  primaryLocal.value = ui.primary
  try {
    const config = await api.getConfig('backup_dir')
    if (config.value) backupDir.value = config.value
  } catch {
    /* 未设置过则留空 */
  }

  try {
    const ol = await api.getOpenListConfig()
    openlist.value = { ...ol, password: '' }
  } catch {
    /* 首次使用时无配置 */
  }
})

async function changeUsername(): Promise<void> {
  if (!usernamePassword.value || !newUsername.value) {
    ui.warn('请填写当前密码与新用户名')
    return
  }
  if (newUsername.value.length < 3) {
    ui.warn('用户名至少 3 个字符')
    return
  }

  savingUsername.value = true
  try {
    const res = await api.changeUsername(usernamePassword.value, newUsername.value)
    if (res.access_token) auth.setSession(res.access_token, res.username)
    usernamePassword.value = ''
    newUsername.value = ''
    ui.success('用户名已修改')
  } catch (err) {
    ui.fail(err, '修改失败')
  } finally {
    savingUsername.value = false
  }
}

async function changePassword(): Promise<void> {
  if (!oldPassword.value || !newPassword.value) {
    ui.warn('请填写完整')
    return
  }
  if (newPassword.value.length < 6) {
    ui.warn('新密码至少 6 个字符')
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    ui.warn('两次输入的新密码不一致')
    return
  }

  savingPassword.value = true
  try {
    const res = await api.changePassword(oldPassword.value, newPassword.value)
    if (res.access_token) auth.setSession(res.access_token, res.username || auth.username || '')
    oldPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
    ui.success('密码已修改，旧会话已失效')
  } catch (err) {
    ui.fail(err, '修改失败')
  } finally {
    savingPassword.value = false
  }
}

function applyPrimary(): void {
  ui.setPrimary(primaryLocal.value)
  ui.success('主题色已更新')
}

async function saveBackupDir(): Promise<void> {
  if (!backupDir.value.trim()) {
    ui.warn('请输入备份目录')
    return
  }
  savingDir.value = true
  try {
    await api.updateConfig('backup_dir', backupDir.value.trim())
    ui.success('已保存。注意：该值仅作为记录，实际生效路径由服务端 BACKUP_DIR 环境变量决定。')
  } catch (err) {
    ui.fail(err, '保存失败')
  } finally {
    savingDir.value = false
  }
}

async function saveOpenList(): Promise<void> {
  if (openlist.value.enabled && !openlist.value.base_url.trim()) {
    ui.warn('启用上传前必须填写服务地址')
    return
  }

  savingOpenlist.value = true
  openlistTestResult.value = null
  try {
    const saved = await api.saveOpenListConfig({
      enabled: openlist.value.enabled,
      base_url: openlist.value.base_url.trim(),
      username: openlist.value.username.trim(),
      // 留空表示不修改已保存的密码
      password: openlist.value.password || null,
      remote_dir: openlist.value.remote_dir.trim() || '/',
      verify_ssl: openlist.value.verify_ssl,
    })
    openlist.value = { ...saved, password: '' }
    ui.success('OpenList 配置已保存')
  } catch (err) {
    ui.fail(err, '保存失败')
  } finally {
    savingOpenlist.value = false
  }
}

async function testOpenList(): Promise<void> {
  testingOpenlist.value = true
  openlistTestResult.value = null
  try {
    const result = await api.testOpenList({
      base_url: openlist.value.base_url.trim() || null,
      username: openlist.value.username.trim() || null,
      password: openlist.value.password || null,
      remote_dir: openlist.value.remote_dir.trim() || null,
      verify_ssl: openlist.value.verify_ssl,
    })
    openlistTestResult.value = { success: result.success, message: result.message }
  } catch (err) {
    openlistTestResult.value = {
      success: false,
      message: err instanceof Error ? err.message : '测试失败',
    }
  } finally {
    testingOpenlist.value = false
  }
}

async function loadAppLogs(): Promise<void> {
  loadingLogs.value = true
  try {
    appLogs.value = await api.appLogs(logLines.value, logLevel.value || undefined)
  } catch (err) {
    ui.fail(err, '读取日志失败')
  } finally {
    loadingLogs.value = false
  }
}
</script>

<template>
  <div>
    <!-- 账户 -->
    <div class="section">
      <h2 style="margin-bottom: 14px">账户设置</h2>
      <div class="row-2" style="align-items: start">
        <div class="card">
          <h3 style="font-size: 14px; margin-bottom: 14px">修改用户名</h3>
          <div class="field" style="margin-bottom: 12px">
            <label>当前密码</label>
            <input v-model="usernamePassword" type="password" autocomplete="current-password" />
          </div>
          <div class="field" style="margin-bottom: 14px">
            <label>新用户名</label>
            <input v-model="newUsername" type="text" minlength="3" />
          </div>
          <button class="btn btn-primary" type="button" :disabled="savingUsername" @click="changeUsername">
            <span v-if="savingUsername" class="spinner" />
            确认修改
          </button>
          <div class="small muted" style="margin-top: 10px">
            修改后当前令牌会失效，本页面已自动更新会话。
          </div>
        </div>

        <div class="card">
          <h3 style="font-size: 14px; margin-bottom: 14px">修改密码</h3>
          <div class="field" style="margin-bottom: 12px">
            <label>当前密码</label>
            <input v-model="oldPassword" type="password" autocomplete="current-password" />
          </div>
          <div class="field" style="margin-bottom: 12px">
            <label>新密码</label>
            <input v-model="newPassword" type="password" autocomplete="new-password" minlength="6" />
          </div>
          <div class="field" style="margin-bottom: 14px">
            <label>确认新密码</label>
            <input v-model="confirmPassword" type="password" autocomplete="new-password" minlength="6" />
          </div>
          <button class="btn btn-primary" type="button" :disabled="savingPassword" @click="changePassword">
            <span v-if="savingPassword" class="spinner" />
            确认修改
          </button>
        </div>
      </div>
    </div>

    <!-- 外观 -->
    <div class="section">
      <h2 style="margin-bottom: 14px">外观</h2>
      <div class="card" style="max-width: 520px">
        <div style="margin-bottom: 18px">
          <div style="font-weight: 500; margin-bottom: 4px">主题模式</div>
          <div class="small muted" style="margin-bottom: 10px">
            「跟随系统」会随操作系统外观自动切换
          </div>
          <div class="row" style="gap: 6px; flex-wrap: wrap">
            <button
              v-for="option in THEME_OPTIONS"
              :key="option.value"
              class="btn btn-sm"
              :class="ui.mode === option.value ? 'btn-primary' : 'btn-outline'"
              type="button"
              @click="ui.setMode(option.value)"
            >
              <Icon :name="option.icon" :size="14" />
              {{ option.label }}
            </button>
          </div>
        </div>

        <div class="row-between">
          <div>
            <div style="font-weight: 500">主题色</div>
            <div class="small muted">用于按钮、链接与高亮</div>
          </div>
          <div class="row">
            <input
              v-model="primaryLocal"
              type="color"
              style="width: 42px; height: 32px; padding: 2px; cursor: pointer"
              @change="applyPrimary"
            />
            <code class="small">{{ primaryLocal }}</code>
          </div>
        </div>
      </div>
    </div>

    <!-- 系统 -->
    <div class="section">
      <h2 style="margin-bottom: 14px">系统</h2>
      <div class="card" style="max-width: 640px">
        <div class="field">
          <label>备份目录（记录值）</label>
          <div class="row">
            <input v-model="backupDir" type="text" class="grow" placeholder="./backups" />
            <button class="btn btn-outline" type="button" :disabled="savingDir" @click="saveBackupDir">
              <span v-if="savingDir" class="spinner" />
              保存
            </button>
          </div>
          <span class="hint">
            修改此值不会改变服务进程实际使用的目录；实际路径由服务端
            <code>BACKUP_DIR</code> 环境变量决定，可在「概览」页查看当前生效值。
          </span>
        </div>
      </div>
    </div>

    <!-- OpenList 上传 -->
    <div class="section">
      <h2 style="margin-bottom: 14px">备份上传（OpenList）</h2>
      <div class="card" style="max-width: 640px">
        <div class="row-between" style="margin-bottom: 18px">
          <div>
            <div style="font-weight: 500">启用上传</div>
            <div class="small muted">
              开启后，备份计划中勾选了「上传」的任务会把备份文件推到 OpenList
            </div>
          </div>
          <label class="switch">
            <input v-model="openlist.enabled" type="checkbox" />
            <span class="track" />
          </label>
        </div>

        <div class="field" style="margin-bottom: 12px">
          <label>服务地址 *</label>
          <input v-model="openlist.base_url" type="text" placeholder="http://192.168.1.10:5244" />
          <span class="hint">OpenList 的访问地址，不要带结尾斜杠</span>
        </div>

        <div class="row-2" style="margin-bottom: 12px">
          <div class="field">
            <label>用户名 *</label>
            <input v-model="openlist.username" type="text" autocomplete="off" />
          </div>
          <div class="field">
            <label>
              密码
              <template v-if="openlist.password_set">（留空则不修改）</template>
            </label>
            <input
              v-model="openlist.password"
              type="password"
              autocomplete="new-password"
              :placeholder="openlist.password_set ? '已保存，留空保持不变' : '请输入密码'"
            />
          </div>
        </div>

        <div class="field" style="margin-bottom: 12px">
          <label>默认远程目录 *</label>
          <input v-model="openlist.remote_dir" type="text" placeholder="/dbsync/backups" />
          <span class="hint">
            备份文件的默认存放目录，不存在时会自动创建。
            单个备份计划可以覆盖此目录。
          </span>
        </div>

        <div class="row-between" style="margin-bottom: 16px">
          <div>
            <div style="font-weight: 500">校验 SSL 证书</div>
            <div class="small muted">自签名证书的内网服务可关闭</div>
          </div>
          <label class="switch">
            <input v-model="openlist.verify_ssl" type="checkbox" />
            <span class="track" />
          </label>
        </div>

        <div v-if="openlistTestResult" class="openlist-result" :class="openlistTestResult.success ? 'ok' : 'fail'">
          <Icon :name="openlistTestResult.success ? 'check' : 'alert'" :size="15" />
          <span>{{ openlistTestResult.message }}</span>
        </div>

        <div class="row" style="gap: 8px">
          <button class="btn btn-primary" type="button" :disabled="savingOpenlist" @click="saveOpenList">
            <span v-if="savingOpenlist" class="spinner" />
            保存配置
          </button>
          <button class="btn btn-outline" type="button" :disabled="testingOpenlist" @click="testOpenList">
            <span v-if="testingOpenlist" class="spinner" />
            {{ testingOpenlist ? '测试中…' : '测试连接' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 应用日志 -->
    <div class="section">
      <h2 style="margin-bottom: 14px">应用日志</h2>
      <div class="card">
        <div class="toolbar" style="margin-bottom: 14px">
          <select v-model.number="logLines" style="width: 120px">
            <option :value="100">最近 100 行</option>
            <option :value="200">最近 200 行</option>
            <option :value="500">最近 500 行</option>
            <option :value="1000">最近 1000 行</option>
          </select>
          <select v-model="logLevel" style="width: 130px">
            <option value="">全部级别</option>
            <option value="INFO">INFO</option>
            <option value="WARNING">WARNING</option>
            <option value="ERROR">ERROR</option>
          </select>
          <button class="btn btn-outline btn-sm" type="button" :disabled="loadingLogs" @click="loadAppLogs">
            <span v-if="loadingLogs" class="spinner" />
            <Icon v-else name="refresh" :size="14" />
            读取
          </button>
          <span v-if="appLogs" class="small muted">
            文件共 {{ appLogs.total_lines }} 行，匹配 {{ appLogs.logs.length }} 行
          </span>
        </div>

        <pre v-if="appLogs && appLogs.logs.length" class="code">{{ appLogs.logs.join('\n') }}</pre>
        <div v-else-if="appLogs" class="empty">没有匹配的日志行</div>
        <div v-else class="empty">点击「读取」加载服务端日志文件</div>
      </div>
    </div>
  </div>
</template>
