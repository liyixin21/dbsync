<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Badge from '@/components/Badge.vue'
import Icon from '@/components/Icon.vue'
import Modal from '@/components/Modal.vue'
import Pager from '@/components/Pager.vue'
import TableState from '@/components/TableState.vue'
import { api } from '@/api/client'
import type { DatabaseConfig, SyncError, SyncTableState, SyncTask } from '@/api/types'
import {
  formatRelative,
  formatSize,
  formatTime,
  healthKind,
  syncHealthText,
  syncStatusKind,
  syncStatusText,
  tableStateKind,
  tableStateText,
} from '@/utils/format'
import { useUiStore } from '@/stores/ui'

const ui = useUiStore()

const tasks = ref<SyncTask[]>([])
const databases = ref<DatabaseConfig[]>([])
const loading = ref(true)
const busyId = ref<number | null>(null)
const search = ref('')

// 创建对话框
const createOpen = ref(false)
const creating = ref(false)
const form = ref({ name: '', source_db_id: 0, target_db_id: 0, auto_start: false })

// 详情面板
const detailTask = ref<SyncTask | null>(null)
const detailTab = ref<'tables' | 'errors'>('tables')
const tableStates = ref<SyncTableState[]>([])
const errors = ref<SyncError[]>([])
const errorPage = ref({ total: 0, skip: 0, limit: 20 })
const detailLoading = ref(false)
const expandedError = ref<number | null>(null)

let pollTimer: number | null = null

/** 缺少可靠定位键的表：同步仍在工作，但 UPDATE/DELETE 有误删风险。 */
const riskyTables = computed(() =>
  tableStates.value
    .filter((t) => !t.pk_columns?.length && !t.unique_keys?.length)
    .map((t) => `${t.schema_name}.${t.table_name}`),
)

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return tasks.value
  return tasks.value.filter((t) =>
    [t.name, t.source_db_name, t.target_db_name].some((v) =>
      (v || '').toLowerCase().includes(q),
    ),
  )
})

async function load(silent = false): Promise<void> {
  if (!silent) loading.value = true
  try {
    const [taskList, dbList] = await Promise.all([api.listSyncTasks(), api.listDatabases()])
    tasks.value = taskList
    databases.value = dbList

    // 详情面板打开时同步刷新其内容
    if (detailTask.value) {
      const refreshed = taskList.find((t) => t.id === detailTask.value!.id)
      if (refreshed) detailTask.value = refreshed
    }
  } catch (err) {
    if (!silent) ui.fail(err, '加载同步任务失败')
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await load()
  // 运行中的任务需要持续观察延迟与健康度
  pollTimer = window.setInterval(() => {
    if (tasks.value.some((t) => t.running)) void load(true)
  }, 5000)
})

onUnmounted(() => {
  if (pollTimer !== null) window.clearInterval(pollTimer)
})

// ---------------------------------------------------------------- 创建

function openCreate(): void {
  if (databases.value.length < 2) {
    ui.warn('请先在「数据库管理」中添加至少两个数据库')
    return
  }
  form.value = {
    name: '',
    source_db_id: databases.value[0].id,
    target_db_id: databases.value[1].id,
    auto_start: false,
  }
  createOpen.value = true
}

async function create(): Promise<void> {
  if (!form.value.name.trim()) {
    ui.warn('请输入任务名称')
    return
  }
  if (form.value.source_db_id === form.value.target_db_id) {
    ui.warn('源数据库与目标数据库不能相同')
    return
  }

  creating.value = true
  try {
    await api.createSyncTask({
      name: form.value.name.trim(),
      source_db_id: form.value.source_db_id,
      target_db_id: form.value.target_db_id,
      auto_start: form.value.auto_start,
    })
    ui.success('同步任务已创建')
    createOpen.value = false
    await load()
  } catch (err) {
    ui.fail(err, '创建失败')
  } finally {
    creating.value = false
  }
}

// ---------------------------------------------------------------- 启停

async function start(task: SyncTask): Promise<void> {
  busyId.value = task.id
  ui.setLoading('正在启动同步任务…')
  try {
    await api.startSyncTask(task.id)
    ui.success('任务已启动')
    await load()
  } catch (err) {
    // binlog_format 不合规等配置问题会在这里给出可操作的原因
    ui.fail(err, '启动失败')
  } finally {
    busyId.value = null
    ui.clearLoading()
  }
}

async function stop(task: SyncTask): Promise<void> {
  busyId.value = task.id
  ui.setLoading('正在停止并保存位点…')
  try {
    await api.stopSyncTask(task.id)
    ui.success('任务已停止')
    await load()
  } catch (err) {
    ui.fail(err, '停止失败')
  } finally {
    busyId.value = null
    ui.clearLoading()
  }
}

async function remove(task: SyncTask): Promise<void> {
  if (!window.confirm(`确定删除同步任务「${task.name}」吗？其表状态与失败事件也会一并删除。`)) return
  try {
    await api.deleteSyncTask(task.id)
    ui.success('已删除')
    if (detailTask.value?.id === task.id) detailTask.value = null
    await load()
  } catch (err) {
    ui.fail(err, '删除失败')
  }
}

async function fullCopy(task: SyncTask): Promise<void> {
  const ok = window.confirm(
    `将「${task.source_db_name}」的全部数据复制到「${task.target_db_name}」。\n\n` +
      '此操作会覆盖目标库中已有的同名数据，且可能耗时较久。确认继续？',
  )
  if (!ok) return

  busyId.value = task.id
  ui.setLoading('正在全量复制，请勿关闭页面…')
  try {
    const res = await api.fullCopy(task.id)
    ui.success(`${res.message}（${formatSize(res.file_size)}，耗时 ${res.duration}s）`)
  } catch (err) {
    ui.fail(err, '全量复制失败')
  } finally {
    busyId.value = null
    ui.clearLoading()
  }
}

// ---------------------------------------------------------------- 详情

async function openDetail(task: SyncTask): Promise<void> {
  detailTask.value = task
  detailTab.value = 'tables'
  await Promise.all([loadTables(task.id), loadErrors(task.id, 0)])
}

async function loadTables(taskId: number): Promise<void> {
  detailLoading.value = true
  try {
    tableStates.value = await api.taskTables(taskId)
  } catch (err) {
    ui.fail(err, '加载表状态失败')
  } finally {
    detailLoading.value = false
  }
}

async function loadErrors(taskId: number, skip = 0): Promise<void> {
  detailLoading.value = true
  try {
    const page = await api.taskErrors(taskId, skip, errorPage.value.limit)
    errors.value = page.data
    errorPage.value = { total: page.page.total, skip: page.page.skip, limit: page.page.limit }
  } catch (err) {
    ui.fail(err, '加载失败事件失败')
  } finally {
    detailLoading.value = false
  }
}

async function switchTab(tab: 'tables' | 'errors'): Promise<void> {
  detailTab.value = tab
  if (!detailTask.value) return
  if (tab === 'tables') await loadTables(detailTask.value.id)
  else await loadErrors(detailTask.value.id, 0)
}

async function retryError(record: SyncError): Promise<void> {
  if (!detailTask.value) return
  try {
    await api.retryTaskError(detailTask.value.id, record.id)
    ui.success('重放成功')
    await Promise.all([loadErrors(detailTask.value.id, errorPage.value.skip), load(true)])
  } catch (err) {
    ui.fail(err, '重放失败')
  }
}

async function clearErrors(): Promise<void> {
  if (!detailTask.value) return
  if (!window.confirm('清空该任务的全部失败事件记录？此操作不可撤销。')) return
  try {
    const res = await api.clearTaskErrors(detailTask.value.id)
    ui.success(res.message)
    await Promise.all([loadErrors(detailTask.value.id, 0), load(true)])
  } catch (err) {
    ui.fail(err, '清空失败')
  }
}
</script>

<template>
  <div>
    <div class="section-head">
      <div class="toolbar grow">
        <input v-model="search" type="search" placeholder="搜索任务名称…" style="width: 240px" />
      </div>
      <button class="btn btn-primary" type="button" @click="openCreate">
        <Icon name="plus" :size="15" />
        创建同步任务
      </button>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>任务</th>
            <th>源 → 目标</th>
            <th>状态</th>
            <th>健康度</th>
            <th>延迟</th>
            <th>已应用</th>
            <th>未应用</th>
            <th>最后同步</th>
            <th style="width: 140px"></th>
          </tr>
        </thead>
        <tbody>
          <TableState
            :cols="9"
            :loading="loading"
            :count="filtered.length"
            empty-text="暂无同步任务"
          />
          <tr v-for="task in filtered" :key="task.id">
            <td>
              <strong>{{ task.name }}</strong>
              <div v-if="task.auto_start" class="small muted">自动启动</div>
            </td>
            <td class="small">
              {{ task.source_db_name || '?' }}
              <span class="muted">→</span>
              {{ task.target_db_name || '?' }}
            </td>
            <td>
              <Badge :kind="syncStatusKind(task.status)" dot :pulse="task.running">
                {{ syncStatusText(task.status) }}
              </Badge>
            </td>
            <td>
              <Badge :kind="healthKind(task.health)">{{ syncHealthText(task.health) }}</Badge>
            </td>
            <td class="cell-mono">{{ task.sync_delay ? task.sync_delay + ' ms' : '—' }}</td>
            <td class="cell-mono">{{ task.applied_events }}</td>
            <td>
              <a
                v-if="task.dlq_count > 0"
                href="#"
                class="text-warning"
                @click.prevent="openDetail(task).then(() => switchTab('errors'))"
              >
                {{ task.dlq_count }}
              </a>
              <span v-else class="muted">0</span>
            </td>
            <td class="small muted">{{ formatRelative(task.last_sync_time) }}</td>
            <td class="actions">
              <button
                v-if="!task.running"
                class="icon-btn"
                type="button"
                title="启动"
                :disabled="busyId === task.id"
                @click="start(task)"
              >
                <Icon name="play" :size="15" />
              </button>
              <button
                v-else
                class="icon-btn"
                type="button"
                title="停止"
                :disabled="busyId === task.id"
                @click="stop(task)"
              >
                <Icon name="stop" :size="15" />
              </button>
              <button
                class="icon-btn"
                type="button"
                title="一键全量复制"
                :disabled="busyId === task.id"
                @click="fullCopy(task)"
              >
                <Icon name="copy" :size="15" />
              </button>
              <button class="icon-btn" type="button" title="详情" @click="openDetail(task)">
                <Icon name="eye" :size="15" />
              </button>
              <button
                class="icon-btn is-danger"
                type="button"
                title="删除"
                @click="remove(task)"
              >
                <Icon name="trash" :size="15" />
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 任务异常原因 -->
    <div
      v-for="task in tasks.filter((t) => t.error_message)"
      :key="`err-${task.id}`"
      class="card"
      style="margin-top: 12px; border-color: var(--danger)"
    >
      <div class="row" style="align-items: flex-start">
        <Icon name="alert" :size="16" style="color: var(--danger); margin-top: 2px" />
        <div class="grow">
          <strong>{{ task.name }}</strong>
          <div class="small" style="margin-top: 4px; white-space: pre-wrap">{{ task.error_message }}</div>
        </div>
      </div>
    </div>

    <!-- 创建对话框 -->
    <Modal
      v-if="createOpen"
      title="创建同步任务"
      confirm-text="创建"
      :busy="creating"
      @close="createOpen = false"
      @confirm="create"
    >
      <div class="field">
        <label>任务名称 *</label>
        <input v-model="form.name" type="text" placeholder="例如：主库 → 从库实时同步" />
      </div>

      <div class="field">
        <label>源数据库 *</label>
        <select v-model.number="form.source_db_id">
          <option v-for="db in databases" :key="db.id" :value="db.id">
            {{ db.name }} ({{ db.host }}:{{ db.port }}/{{ db.database_name }})
          </option>
        </select>
      </div>

      <div class="field">
        <label>目标数据库 *</label>
        <select v-model.number="form.target_db_id">
          <option v-for="db in databases" :key="db.id" :value="db.id">
            {{ db.name }} ({{ db.host }}:{{ db.port }}/{{ db.database_name }})
          </option>
        </select>
        <span class="hint">目标库必须已存在同名表结构，DDL 也会被同步</span>
      </div>

      <div class="row">
        <label class="switch">
          <input v-model="form.auto_start" type="checkbox" />
          <span class="track" />
        </label>
        <span>服务重启后自动启动该任务</span>
      </div>

      <div class="card" style="background: var(--bg-subtle)">
        <div class="small muted">
          启动前会校验源库的 <code>binlog_format</code> 必须为 <code>ROW</code>、
          <code>binlog_row_image</code> 必须为 <code>FULL</code>。
          不满足时任务会拒绝启动并给出修复指引，避免数据被静默丢弃。
        </div>
      </div>
    </Modal>

    <!-- 详情面板 -->
    <Modal
      v-if="detailTask"
      :title="`任务详情 · ${detailTask.name}`"
      @close="detailTask = null"
    >
      <template #footer>
        <button class="btn btn-ghost" type="button" @click="detailTask = null">关闭</button>
      </template>

      <dl class="kv">
        <dt>状态</dt>
        <dd>
          <Badge :kind="syncStatusKind(detailTask.status)">{{ syncStatusText(detailTask.status) }}</Badge>
          <Badge :kind="healthKind(detailTask.health)">{{ syncHealthText(detailTask.health) }}</Badge>
        </dd>
        <dt>当前位点</dt>
        <dd class="mono">
          <template v-if="detailTask.gtid_set">{{ detailTask.gtid_set }}</template>
          <template v-else-if="detailTask.binlog_file">
            {{ detailTask.binlog_file }} @ {{ detailTask.binlog_position }}
          </template>
          <template v-else>尚未开始</template>
        </dd>
        <dt>源库 binlog</dt>
        <dd class="mono">
          format={{ detailTask.binlog_format || '?' }} · row_image={{ detailTask.binlog_row_image || '?' }}
        </dd>
        <dt>已应用事件</dt>
        <dd>{{ detailTask.applied_events }}</dd>
        <dt>未应用事件</dt>
        <dd :class="detailTask.dlq_count ? 'text-warning' : ''">{{ detailTask.dlq_count }}</dd>
      </dl>

      <div class="row" style="gap: 4px; border-bottom: 1px solid var(--border)">
        <button
          class="btn btn-ghost btn-sm"
          :style="detailTab === 'tables' ? 'color: var(--primary)' : ''"
          type="button"
          @click="switchTab('tables')"
        >
          表状态（{{ tableStates.length }}）
        </button>
        <button
          class="btn btn-ghost btn-sm"
          :style="detailTab === 'errors' ? 'color: var(--primary)' : ''"
          type="button"
          @click="switchTab('errors')"
        >
          失败事件（{{ errorPage.total }}）
        </button>
        <span class="grow" />
        <button
          v-if="detailTab === 'errors' && errors.length"
          class="btn btn-ghost btn-sm"
          type="button"
          @click="clearErrors"
        >
          清空
        </button>
      </div>

      <!-- 表状态 -->
      <div v-if="detailTab === 'tables'">
        <div v-if="detailLoading" class="loading-block"><span class="spinner" />加载中…</div>
        <div v-else-if="!tableStates.length" class="empty">
          尚未同步任何表。任务启动并有数据变更后，这里会列出每张表的状态。
        </div>
        <div v-else>
          <!-- 无主键风险说明：不是错误，但需要用户知晓 -->
          <div v-if="riskyTables.length" class="card" style="border-color: var(--warning); margin-bottom: 10px; padding: 11px 13px">
            <div class="row" style="align-items: flex-start; gap: 9px">
              <Icon name="alert" :size="16" style="color: var(--warning); margin-top: 2px" />
              <div class="grow small">
                <strong>{{ riskyTables.length }} 张表没有主键或唯一键</strong>
                <div class="muted" style="margin-top: 4px; line-height: 1.5">
                  这些表的 UPDATE/DELETE 只能靠「全列值匹配」定位。若表中存在完全相同的多行，
                  删除其中一行时会连带删除目标库中的其它相同行，造成数据不一致。
                  建议为这些表添加主键：
                  <span class="mono">{{ riskyTables.join('、') }}</span>
                </div>
              </div>
            </div>
          </div>

          <div class="table-wrap" style="max-height: 320px; overflow-y: auto">
            <table>
              <thead>
                <tr>
                  <th>表</th>
                  <th>状态</th>
                  <th>定位键</th>
                  <th>已应用</th>
                  <th>最后活动</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="s in tableStates" :key="`${s.schema_name}.${s.table_name}`">
                  <td class="mono small">{{ s.schema_name }}.{{ s.table_name }}</td>
                  <td><Badge :kind="tableStateKind(s.state)">{{ tableStateText(s.state) }}</Badge></td>
                  <td class="mono small">
                    <template v-if="s.pk_columns?.length">
                      主键 {{ s.pk_columns.join(', ') }}
                    </template>
                    <template v-else-if="s.unique_keys?.length">
                      唯一键 {{ s.unique_keys[0]?.join(', ') }}
                    </template>
                    <span v-else class="text-warning">无（全列匹配）</span>
                  </td>
                  <td class="cell-mono">{{ s.applied_events }}</td>
                  <td class="small muted">{{ formatRelative(s.last_applied_at) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- 失败事件 -->
      <div v-else>
        <div v-if="detailLoading" class="loading-block"><span class="spinner" />加载中…</div>
        <div v-else-if="!errors.length" class="empty">没有未处理的失败事件</div>
        <div v-else>
          <div
            v-for="record in errors"
            :key="record.id"
            class="card"
            style="margin-bottom: 8px; padding: 12px"
          >
            <div class="row-between">
              <div class="grow">
                <div class="row" style="gap: 6px; flex-wrap: wrap">
                  <Badge kind="danger">{{ record.event_type.toUpperCase() }}</Badge>
                  <code class="small">{{ record.schema_name }}.{{ record.table_name }}</code>
                  <span v-if="record.error_code" class="small muted">错误码 {{ record.error_code }}</span>
                  <span class="small muted">{{ formatTime(record.created_at) }}</span>
                </div>
                <div class="small" style="margin-top: 6px">{{ record.error_message }}</div>
              </div>
              <div class="row" style="gap: 4px">
                <button
                  class="btn btn-outline btn-sm"
                  type="button"
                  @click="expandedError = expandedError === record.id ? null : record.id"
                >
                  {{ expandedError === record.id ? '收起' : '数据' }}
                </button>
                <button
                  class="btn btn-outline btn-sm"
                  type="button"
                  :disabled="!detailTask.running"
                  :title="detailTask.running ? '重放该事件' : '任务未运行，无法重放'"
                  @click="retryError(record)"
                >
                  重放
                </button>
              </div>
            </div>
            <pre v-if="expandedError === record.id" class="code" style="margin-top: 10px">{{ record.payload }}</pre>
          </div>

          <Pager
            :total="errorPage.total"
            :skip="errorPage.skip"
            :limit="errorPage.limit"
            @change="(s) => detailTask && loadErrors(detailTask.id, s)"
          />
        </div>
      </div>
    </Modal>
  </div>
</template>
