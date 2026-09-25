<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Badge from '@/components/Badge.vue'
import Icon from '@/components/Icon.vue'
import Modal from '@/components/Modal.vue'
import Pager from '@/components/Pager.vue'
import TableState from '@/components/TableState.vue'
import { api } from '@/api/client'
import type {
  BackupHistoryItem,
  BackupPlan,
  BackupPlanPayload,
  BackupStatistics,
  DatabaseConfig,
} from '@/api/types'
import {
  backupStatusKind,
  backupStatusText,
  formatDuration,
  formatSize,
  formatTime,
} from '@/utils/format'
import { useUiStore } from '@/stores/ui'

const ui = useUiStore()

const plans = ref<BackupPlan[]>([])
const databases = ref<DatabaseConfig[]>([])
const statistics = ref<BackupStatistics | null>(null)
const history = ref<BackupHistoryItem[]>([])
const page = ref({ total: 0, skip: 0, limit: 20 })

const loadingPlans = ref(true)
const loadingHistory = ref(true)
const busyId = ref<number | null>(null)

// 计划对话框
const dialogOpen = ref(false)
const editingId = ref<number | null>(null)
const saving = ref(false)
const form = ref<BackupPlanPayload>({
  name: '',
  database_id: 0,
  schedule_interval: 60,
  retention_count: 50,
  is_active: true,
  upload_enabled: false,
  upload_dir: '',
})

// 恢复对话框
const restoreOpen = ref(false)
const restoreTarget = ref<BackupHistoryItem | null>(null)
const restoreDbId = ref<number | null>(null)
const restoring = ref(false)

const planNames = computed(() => {
  const map: Record<number, string> = {}
  for (const plan of plans.value) map[plan.id] = plan.name
  return map
})

async function loadPlans(): Promise<void> {
  loadingPlans.value = true
  try {
    const [planList, dbList] = await Promise.all([api.listBackupPlans(), api.listDatabases()])
    plans.value = planList
    databases.value = dbList
  } catch (err) {
    ui.fail(err, '加载备份计划失败')
  } finally {
    loadingPlans.value = false
  }
}

async function loadHistory(skip = 0): Promise<void> {
  loadingHistory.value = true
  try {
    const [pageRes, stats] = await Promise.all([
      api.listBackupHistory(skip, page.value.limit),
      api.backupStatistics(),
    ])
    history.value = pageRes.data
    page.value = { total: pageRes.page.total, skip: pageRes.page.skip, limit: pageRes.page.limit }
    statistics.value = stats
  } catch (err) {
    ui.fail(err, '加载备份历史失败')
  } finally {
    loadingHistory.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadPlans(), loadHistory(0)])
})

// ---------------------------------------------------------------- 计划

function openCreate(): void {
  if (!databases.value.length) {
    ui.warn('请先在「数据库管理」中添加数据库')
    return
  }
  editingId.value = null
  form.value = {
    name: '',
    database_id: databases.value[0].id,
    schedule_interval: 60,
    retention_count: 50,
    is_active: true,
    upload_enabled: false,
    upload_dir: '',
  }
  dialogOpen.value = true
}

function openEdit(plan: BackupPlan): void {
  editingId.value = plan.id
  form.value = {
    name: plan.name,
    database_id: plan.database_id,
    schedule_interval: plan.schedule_interval,
    retention_count: plan.retention_count,
    is_active: plan.is_active,
    upload_enabled: plan.upload_enabled,
    upload_dir: plan.upload_dir || '',
  }
  dialogOpen.value = true
}

async function save(): Promise<void> {
  if (!form.value.name.trim()) {
    ui.warn('请输入计划名称')
    return
  }
  if (!form.value.schedule_interval || form.value.schedule_interval < 1) {
    ui.warn('请设置备份间隔（分钟）')
    return
  }

  saving.value = true
  try {
    if (editingId.value !== null) {
      await api.updateBackupPlan(editingId.value, form.value)
      ui.success('备份计划已更新')
    } else {
      await api.createBackupPlan(form.value)
      ui.success('备份计划已创建')
    }
    dialogOpen.value = false
    await loadPlans()
  } catch (err) {
    ui.fail(err, '保存失败')
  } finally {
    saving.value = false
  }
}

async function toggleActive(plan: BackupPlan): Promise<void> {
  try {
    await api.updateBackupPlan(plan.id, { is_active: !plan.is_active })
    ui.success(plan.is_active ? '已禁用' : '已启用')
    await loadPlans()
  } catch (err) {
    ui.fail(err, '操作失败')
    await loadPlans()
  }
}

async function executeNow(plan: BackupPlan): Promise<void> {
  busyId.value = plan.id
  ui.setLoading(`正在执行备份「${plan.name}」…`)
  try {
    const res = await api.executeBackupPlan(plan.id)
    ui.success(`备份完成${res.history_id ? `（记录 #${res.history_id}）` : ''}`)
    await Promise.all([loadHistory(0), loadPlans()])
  } catch (err) {
    ui.fail(err, '备份执行失败')
    await loadHistory(0)
  } finally {
    busyId.value = null
    ui.clearLoading()
  }
}

async function removePlan(plan: BackupPlan): Promise<void> {
  const ok = window.confirm(
    `确定删除备份计划「${plan.name}」吗？\n\n其备份历史记录与磁盘上的备份文件也会一并删除。`,
  )
  if (!ok) return
  try {
    const res = await api.deleteBackupPlan(plan.id, true)
    ui.success(res.deleted_files ? `已删除计划及 ${res.deleted_files} 个备份文件` : '已删除计划')
    await Promise.all([loadPlans(), loadHistory(0)])
  } catch (err) {
    ui.fail(err, '删除失败')
  }
}

// ---------------------------------------------------------------- 历史

/** 上传状态 → 徽章类别。 */
function uploadKind(status: string): 'neutral' | 'primary' | 'success' | 'danger' | 'warning' {
  switch (status) {
    case 'success':
      return 'success'
    case 'failed':
      return 'danger'
    case 'uploading':
      return 'primary'
    case 'pending':
      return 'warning'
    default:
      return 'neutral'
  }
}

function uploadText(status: string): string {
  switch (status) {
    case 'success':
      return '已上传'
    case 'failed':
      return '上传失败'
    case 'uploading':
      return '上传中'
    case 'pending':
      return '待上传'
    default:
      return '未上传'
  }
}

async function uploadToOpenList(item: BackupHistoryItem): Promise<void> {
  const again = item.upload_status === 'success'
  const tip = again
    ? '该备份已上传过，确认重新上传？'
    : '将备份文件上传到 OpenList？'
  if (!window.confirm(tip)) return

  busyId.value = item.id
  ui.setLoading('正在上传到 OpenList…')
  try {
    const res = await api.uploadBackup(item.id)
    ui.success(res.message)
    await loadHistory(page.value.skip)
  } catch (err) {
    ui.fail(err, '上传失败')
    await loadHistory(page.value.skip)
  } finally {
    busyId.value = null
    ui.clearLoading()
  }
}

async function download(item: BackupHistoryItem): Promise<void> {
  try {
    const blob = await api.downloadBackup(item.id)
    const { saveBlob } = await import('@/utils/format')
    saveBlob(blob, item.file_path?.split('/').pop() || `backup_${item.id}.sql`)
    ui.success('下载已开始')
  } catch (err) {
    ui.fail(err, '下载失败')
  }
}

function openRestore(item: BackupHistoryItem): void {
  restoreTarget.value = item
  restoreDbId.value = null
  restoreOpen.value = true
}

async function doRestore(): Promise<void> {
  if (!restoreTarget.value) return
  restoring.value = true
  ui.setLoading('正在恢复数据库，请勿关闭页面…')
  try {
    const res = await api.restoreBackup(
      restoreTarget.value.id,
      restoreDbId.value ?? undefined,
    )
    ui.success(res.message)
    restoreOpen.value = false
    await Promise.all([loadHistory(page.value.skip), loadPlans()])
  } catch (err) {
    ui.fail(err, '恢复失败')
  } finally {
    restoring.value = false
    ui.clearLoading()
  }
}

async function removeHistory(item: BackupHistoryItem): Promise<void> {
  if (!window.confirm('删除这条备份记录？磁盘上的备份文件也会被删除。')) return
  try {
    await api.deleteBackupHistory(item.id, true)
    ui.success('已删除')
    await loadHistory(page.value.skip)
  } catch (err) {
    ui.fail(err, '删除失败')
  }
}

async function clearHistory(): Promise<void> {
  const ok = window.confirm(
    '清空全部备份历史记录？\n\n磁盘上对应的备份文件也会被删除，此操作不可撤销。',
  )
  if (!ok) return
  try {
    const res = await api.clearBackupHistory(true)
    ui.success(res.message)
    await loadHistory(0)
  } catch (err) {
    ui.fail(err, '清空失败')
  }
}
</script>

<template>
  <div>
    <!-- 备份计划 -->
    <div class="section">
      <div class="section-head">
        <h2>备份计划</h2>
        <button class="btn btn-primary" type="button" @click="openCreate">
          <Icon name="plus" :size="15" />
          创建备份计划
        </button>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>计划名称</th>
              <th>数据库</th>
              <th>间隔</th>
              <th>保留</th>
              <th>启用</th>
              <th>上次执行</th>
              <th>下次执行</th>
              <th style="width: 120px"></th>
            </tr>
          </thead>
          <tbody>
            <TableState
              :cols="8"
              :loading="loadingPlans"
              :count="plans.length"
              empty-text="暂无备份计划"
            />
            <tr v-for="plan in plans" :key="plan.id">
              <td>
                <strong>{{ plan.name }}</strong>
                <Badge v-if="plan.running" kind="primary" style="margin-left: 6px">执行中</Badge>
              </td>
              <td class="small">{{ plan.database_name || `#${plan.database_id}` }}</td>
              <td class="cell-mono">每 {{ plan.schedule_interval }} 分钟</td>
              <td class="cell-mono">{{ plan.retention_count }} 份</td>
              <td>
                <label class="switch">
                  <input
                    type="checkbox"
                    :checked="plan.is_active"
                    @change="toggleActive(plan)"
                  />
                  <span class="track" />
                </label>
              </td>
              <td class="small muted">{{ formatTime(plan.last_run_at) }}</td>
              <td class="small muted">{{ formatTime(plan.next_run_at) }}</td>
              <td class="actions">
                <button
                  class="icon-btn"
                  type="button"
                  title="立即备份"
                  :disabled="busyId === plan.id"
                  @click="executeNow(plan)"
                >
                  <Icon name="play" :size="15" />
                </button>
                <button class="icon-btn" type="button" title="编辑" @click="openEdit(plan)">
                  <Icon name="edit" :size="15" />
                </button>
                <button
                  class="icon-btn is-danger"
                  type="button"
                  title="删除"
                  @click="removePlan(plan)"
                >
                  <Icon name="trash" :size="15" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 备份统计 -->
    <div v-if="statistics" class="section">
      <div class="stats">
        <div class="stat">
          <div class="label">备份记录</div>
          <div class="value">{{ statistics.total_count }}</div>
          <div class="sub">近 7 天 {{ statistics.recent_count }} 次</div>
        </div>
        <div class="stat">
          <div class="label">备份总大小</div>
          <div class="value">{{ formatSize(statistics.total_size) }}</div>
          <div class="sub">按记录累计</div>
        </div>
        <div class="stat">
          <div class="label">磁盘文件</div>
          <div class="value">{{ statistics.disk_file_count }}</div>
          <div class="sub">实际存在的备份文件</div>
        </div>
        <div class="stat">
          <div class="label">孤儿文件</div>
          <div class="value" :class="statistics.orphan_file_count ? 'text-warning' : ''">
            {{ statistics.orphan_file_count }}
          </div>
          <div class="sub">无对应记录的 .sql 文件</div>
        </div>
      </div>
    </div>

    <!-- 备份历史 -->
    <div class="section">
      <div class="section-head">
        <h2>备份历史</h2>
        <button
          v-if="history.length"
          class="btn btn-outline btn-sm"
          type="button"
          @click="clearHistory"
        >
          <Icon name="trash" :size="14" />
          清空历史
        </button>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>计划</th>
              <th>状态</th>
              <th>大小</th>
              <th>耗时</th>
              <th>触发</th>
              <th>上传</th>
              <th style="width: 160px"></th>
            </tr>
          </thead>
          <tbody>
            <TableState
              :cols="8"
              :loading="loadingHistory"
              :count="history.length"
              empty-text="暂无备份记录"
            />
            <tr v-for="item in history" :key="item.id">
              <td class="small">{{ formatTime(item.created_at) }}</td>
              <td>{{ item.plan_name || planNames[item.backup_plan_id] || `#${item.backup_plan_id}` }}</td>
              <td>
                <Badge :kind="backupStatusKind(item.status)">
                  {{ backupStatusText(item.status) }}
                </Badge>
                <div v-if="item.error_message" class="small text-danger" style="margin-top: 4px">
                  {{ item.error_message }}
                </div>
              </td>
              <td class="cell-mono">{{ formatSize(item.file_size) }}</td>
              <td class="cell-mono">{{ formatDuration(item.duration) }}</td>
              <td class="small muted">{{ item.trigger === 'manual' ? '手动' : '定时' }}</td>
              <td>
                <Badge :kind="uploadKind(item.upload_status)" :title="item.upload_error || item.upload_path || ''">
                  {{ uploadText(item.upload_status) }}
                </Badge>
              </td>
              <td class="actions">
                <button
                  v-if="item.status === 'completed' && item.file_exists"
                  class="icon-btn"
                  type="button"
                  :title="item.upload_status === 'success' ? '重新上传到 OpenList' : '上传到 OpenList'"
                  @click="uploadToOpenList(item)"
                >
                  <Icon name="upload" :size="15" />
                </button>
                <button
                  v-if="item.status === 'completed' && item.file_exists"
                  class="icon-btn"
                  type="button"
                  title="下载"
                  @click="download(item)"
                >
                  <Icon name="download" :size="15" />
                </button>
                <button
                  v-if="item.status === 'completed' && item.file_exists"
                  class="icon-btn"
                  type="button"
                  title="恢复"
                  @click="openRestore(item)"
                >
                  <Icon name="restore" :size="15" />
                </button>
                <button
                  class="icon-btn is-danger"
                  type="button"
                  title="删除"
                  @click="removeHistory(item)"
                >
                  <Icon name="trash" :size="15" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <Pager
        :total="page.total"
        :skip="page.skip"
        :limit="page.limit"
        @change="loadHistory"
      />
    </div>

    <!-- 计划对话框 -->
    <Modal
      v-if="dialogOpen"
      :title="editingId !== null ? '编辑备份计划' : '创建备份计划'"
      :confirm-text="editingId !== null ? '保存' : '创建'"
      :busy="saving"
      @close="dialogOpen = false"
      @confirm="save"
    >
      <div class="field">
        <label>计划名称 *</label>
        <input v-model="form.name" type="text" placeholder="例如：生产库每日全量备份" />
      </div>

      <div class="field">
        <label>数据库 *</label>
        <select v-model.number="form.database_id">
          <option v-for="db in databases" :key="db.id" :value="db.id">
            {{ db.name }} ({{ db.host }}:{{ db.port }}/{{ db.database_name }})
          </option>
        </select>
      </div>

      <div class="row-2">
        <div class="field">
          <label>备份间隔（分钟）*</label>
          <input v-model.number="form.schedule_interval" type="number" min="1" />
        </div>
        <div class="field">
          <label>保留份数 *</label>
          <input v-model.number="form.retention_count" type="number" min="1" />
        </div>
      </div>
      <div class="small muted">
        超过保留份数的旧备份会在每次备份完成后自动清理（记录与文件一并删除）。
      </div>

      <div class="row">
        <label class="switch">
          <input v-model="form.is_active" type="checkbox" />
          <span class="track" />
        </label>
        <span>立即启用调度</span>
      </div>

      <div class="row" style="align-items: flex-start; gap: 10px; padding-top: 14px; border-top: 1px solid var(--border)">
        <label class="switch" style="margin-top: 2px">
          <input v-model="form.upload_enabled" type="checkbox" />
          <span class="track" />
        </label>
        <div class="grow">
          <div style="font-weight: 500">备份后上传到 OpenList</div>
          <div class="small muted">需要先在「设置」页配置并启用 OpenList</div>
        </div>
      </div>

      <div v-if="form.upload_enabled" class="field">
        <label>远程目录（可选）</label>
        <input v-model="form.upload_dir" type="text" placeholder="留空则使用「设置」页的默认目录" />
        <span class="hint">可为该计划单独指定目录，方便按库或按周期归类</span>
      </div>
    </Modal>

    <!-- 恢复确认 -->
    <Modal
      v-if="restoreOpen && restoreTarget"
      title="恢复数据库"
      confirm-text="开始恢复"
      danger
      :busy="restoring"
      @close="restoreOpen = false"
      @confirm="doRestore"
    >
      <div class="card" style="border-color: var(--danger)">
        <div class="row" style="align-items: flex-start">
          <Icon name="alert" :size="16" style="color: var(--danger); margin-top: 2px" />
          <div class="small">
            恢复会用备份文件中的内容覆盖目标数据库的同名数据，此操作不可撤销。
            执行期间相关同步任务会被自动暂停，完成后重新启动。
          </div>
        </div>
      </div>

      <dl class="kv">
        <dt>备份文件</dt>
        <dd class="mono small">{{ restoreTarget.file_path }}</dd>
        <dt>备份大小</dt>
        <dd>{{ formatSize(restoreTarget.file_size) }}</dd>
        <dt>备份时间</dt>
        <dd>{{ formatTime(restoreTarget.created_at) }}</dd>
      </dl>

      <div class="field">
        <label>恢复到哪个数据库</label>
        <select v-model.number="restoreDbId">
          <option :value="null">默认：备份来源库</option>
          <option v-for="db in databases" :key="db.id" :value="db.id">
            {{ db.name }} ({{ db.host }}:{{ db.port }}/{{ db.database_name }})
          </option>
        </select>
      </div>
    </Modal>
  </div>
</template>
