<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Badge from '@/components/Badge.vue'
import Icon from '@/components/Icon.vue'
import Pager from '@/components/Pager.vue'
import TableState from '@/components/TableState.vue'
import { api } from '@/api/client'
import type { LoginLog, LogStatistics, OperationLog, RunLog } from '@/api/types'
import { formatTime, logLevelKind } from '@/utils/format'
import { useUiStore } from '@/stores/ui'

type LogType = 'operations' | 'logins' | 'runs'

const ui = useUiStore()

const type = ref<LogType>('operations')
const search = ref('')
const loading = ref(false)
const statistics = ref<LogStatistics | null>(null)

const operationItems = ref<OperationLog[]>([])
const loginItems = ref<LoginLog[]>([])
const runItems = ref<RunLog[]>([])

const page = ref({ total: 0, skip: 0, limit: 30 })

const TABS: Array<{ value: LogType; label: string; icon: string }> = [
  { value: 'operations', label: '操作日志', icon: 'edit' },
  { value: 'logins', label: '登录日志', icon: 'key' },
  { value: 'runs', label: '运行日志', icon: 'terminal' },
]

const currentCount = computed(() => {
  if (type.value === 'operations') return operationItems.value.length
  if (type.value === 'logins') return loginItems.value.length
  return runItems.value.length
})

async function load(skip = 0): Promise<void> {
  loading.value = true
  try {
    const params = { skip, limit: page.value.limit, search: search.value.trim() || undefined }
    if (type.value === 'operations') {
      const res = await api.operationLogs(params)
      operationItems.value = res.data
      page.value = { total: res.page.total, skip: res.page.skip, limit: res.page.limit }
    } else if (type.value === 'logins') {
      const res = await api.loginLogs(params)
      loginItems.value = res.data
      page.value = { total: res.page.total, skip: res.page.skip, limit: res.page.limit }
    } else {
      const res = await api.runLogs(params)
      runItems.value = res.data
      page.value = { total: res.page.total, skip: res.page.skip, limit: res.page.limit }
    }

    statistics.value = await api.logStatistics()
  } catch (err) {
    ui.fail(err, '加载日志失败')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load(0)
})

function switchType(next: LogType): void {
  type.value = next
  search.value = ''
  void load(0)
}

function doSearch(): void {
  void load(0)
}

async function removeOne(kind: LogType, id: number): Promise<void> {
  if (!window.confirm('删除这条日志？')) return
  try {
    await api.deleteLog(kind, id)
    ui.success('已删除')
    await load(page.value.skip)
  } catch (err) {
    ui.fail(err, '删除失败')
  }
}

async function clearAll(): Promise<void> {
  const label = TABS.find((t) => t.value === type.value)?.label || '日志'
  if (!window.confirm(`清空全部${label}？此操作不可撤销。`)) return
  try {
    const res = await api.clearLogs(type.value)
    ui.success(res.message)
    await load(0)
  } catch (err) {
    ui.fail(err, '清空失败')
  }
}
</script>

<template>
  <div>
    <!-- 统计 -->
    <div v-if="statistics" class="section">
      <div class="stats">
        <div class="stat">
          <div class="label">操作日志</div>
          <div class="value">{{ statistics.operations.total }}</div>
          <div class="sub">今日 {{ statistics.operations.today }}</div>
        </div>
        <div class="stat">
          <div class="label">登录日志</div>
          <div class="value">{{ statistics.logins.total }}</div>
          <div class="sub">
            今日 {{ statistics.logins.today }} · 失败
            <span :class="statistics.logins.failed ? 'text-danger' : ''">
              {{ statistics.logins.failed }}
            </span>
          </div>
        </div>
        <div class="stat">
          <div class="label">运行日志</div>
          <div class="value">{{ statistics.runs.total }}</div>
          <div class="sub">
            错误 <span :class="statistics.runs.errors ? 'text-danger' : ''">{{ statistics.runs.errors }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 工具条 -->
    <div class="section-head">
      <div class="row" style="gap: 2px">
        <button
          v-for="tab in TABS"
          :key="tab.value"
          class="btn btn-sm"
          :class="type === tab.value ? 'btn-outline' : 'btn-ghost'"
          :style="type === tab.value ? 'border-color: var(--primary); color: var(--primary)' : ''"
          type="button"
          @click="switchType(tab.value)"
        >
          <Icon :name="tab.icon" :size="14" />
          {{ tab.label }}
        </button>
      </div>

      <div class="toolbar">
        <input
          v-model="search"
          type="search"
          placeholder="搜索…"
          style="width: 200px"
          @keyup.enter="doSearch"
        />
        <button class="btn btn-outline btn-sm" type="button" @click="doSearch">
          <Icon name="search" :size="14" />
          搜索
        </button>
        <button
          v-if="currentCount"
          class="btn btn-outline btn-sm"
          type="button"
          @click="clearAll"
        >
          <Icon name="trash" :size="14" />
          清空
        </button>
      </div>
    </div>

    <!-- 操作日志 -->
    <div v-if="type === 'operations'" class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>用户</th>
            <th>操作</th>
            <th>资源</th>
            <th>详情</th>
            <th>IP</th>
            <th style="width: 50px"></th>
          </tr>
        </thead>
        <tbody>
          <TableState
            :cols="7"
            :loading="loading"
            :count="page.total"
            empty-text="暂无操作日志"
          />
          <tr v-for="log in operationItems" :key="log.id">
            <td class="small nowrap">{{ formatTime(log.created_at) }}</td>
            <td class="small">{{ log.username || '—' }}</td>
            <td class="small">{{ log.action }}</td>
            <td class="small">
              <Badge v-if="log.resource_type" kind="neutral">{{ log.resource_type }}</Badge>
              <span v-else class="muted">—</span>
            </td>
            <td class="small truncate" :title="log.detail || log.resource_name || ''">
              {{ log.detail || log.resource_name || '—' }}
            </td>
            <td class="small mono">{{ log.ip_address || '—' }}</td>
            <td class="actions">
              <button
                class="icon-btn is-danger"
                type="button"
                title="删除"
                @click="removeOne('operations', log.id)"
              >
                <Icon name="trash" :size="14" />
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 登录日志 -->
    <div v-else-if="type === 'logins'" class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>用户名</th>
            <th>结果</th>
            <th>IP</th>
            <th>原因</th>
            <th style="width: 50px"></th>
          </tr>
        </thead>
        <tbody>
          <TableState
            :cols="6"
            :loading="loading"
            :count="page.total"
            empty-text="暂无登录日志"
          />
          <tr v-for="log in loginItems" :key="log.id">
            <td class="small nowrap">{{ formatTime(log.created_at) }}</td>
            <td class="small">{{ log.username }}</td>
            <td>
              <Badge :kind="log.success ? 'success' : 'danger'">
                {{ log.success ? '成功' : '失败' }}
              </Badge>
            </td>
            <td class="small mono">{{ log.ip_address || '—' }}</td>
            <td class="small muted">{{ log.failure_reason || '—' }}</td>
            <td class="actions">
              <button
                class="icon-btn is-danger"
                type="button"
                title="删除"
                @click="removeOne('logins', log.id)"
              >
                <Icon name="trash" :size="14" />
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 运行日志 -->
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>类型</th>
            <th>任务</th>
            <th>级别</th>
            <th>消息</th>
            <th style="width: 50px"></th>
          </tr>
        </thead>
        <tbody>
          <TableState
            :cols="6"
            :loading="loading"
            :count="page.total"
            empty-text="暂无运行日志"
          />
          <tr v-for="log in runItems" :key="log.id">
            <td class="small nowrap">{{ formatTime(log.created_at) }}</td>
            <td><Badge kind="neutral">{{ log.task_type }}</Badge></td>
            <td class="small">{{ log.task_name || `#${log.task_id}` }}</td>
            <td><Badge :kind="logLevelKind(log.level)">{{ log.level }}</Badge></td>
            <td class="small" style="max-width: 420px">{{ log.message }}</td>
            <td class="actions">
              <button
                class="icon-btn is-danger"
                type="button"
                title="删除"
                @click="removeOne('runs', log.id)"
              >
                <Icon name="trash" :size="14" />
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <Pager :total="page.total" :skip="page.skip" :limit="page.limit" @change="load" />
  </div>
</template>
