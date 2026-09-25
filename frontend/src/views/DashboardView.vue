<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import Badge from '@/components/Badge.vue'
import Icon from '@/components/Icon.vue'
import TableState from '@/components/TableState.vue'
import { api } from '@/api/client'
import type { BackupHistoryItem, SyncTask, SystemStatus } from '@/api/types'
import {
  backupStatusKind,
  backupStatusText,
  formatSize,
  formatTime,
  formatUptime,
  healthKind,
  syncHealthText,
  syncStatusKind,
  syncStatusText,
} from '@/utils/format'
import { useUiStore } from '@/stores/ui'

const ui = useUiStore()
const router = useRouter()

const status = ref<SystemStatus | null>(null)
const syncTasks = ref<SyncTask[]>([])
const recent = ref<BackupHistoryItem[]>([])
const loading = ref(true)

async function load(): Promise<void> {
  loading.value = true
  try {
    const [statusRes, syncRes] = await Promise.all([
      api.systemStatus(),
      api.listSyncTasks(),
    ])
    status.value = statusRes
    syncTasks.value = syncRes

    const history = await api.listBackupHistory(0, 5)
    recent.value = history.data
  } catch (err) {
    ui.fail(err, '加载概览失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 有问题的同步任务：失败、停滞、或积累了未处理事件。 */
const problematic = () => syncTasks.value.filter((t) => t.health === 'stalled' || t.health === 'degraded')

function diskPercent(): number {
  return Math.round(status.value?.disk_usage?.percent ?? 0)
}

function diskClass(): string {
  const p = diskPercent()
  if (p >= 90) return 'text-danger'
  if (p >= 75) return 'text-warning'
  return ''
}

defineExpose({ load })
</script>

<template>
  <div>
    <!-- 异常提醒：旧版本把这些问题完全隐藏在一个「运行中」的状态里 -->
    <div v-if="problematic().length" class="card mb-0" style="margin-bottom: 18px; border-color: var(--warning)">
      <div class="row" style="align-items: flex-start">
        <Icon name="alert" :size="18" style="color: var(--warning); margin-top: 2px" />
        <div class="grow">
          <strong>{{ problematic().length }} 个同步任务需要关注</strong>
          <div class="small muted" style="margin-top: 4px">
            <template v-for="(t, i) in problematic()" :key="t.id">
              <span v-if="i > 0">、</span>
              <a href="#" @click.prevent="router.push({ name: 'sync' })">{{ t.name }}</a>
              （{{ syncHealthText(t.health) }}<template v-if="t.dlq_count">，{{ t.dlq_count }} 条未应用事件</template>）
            </template>
          </div>
        </div>
      </div>
    </div>

    <!-- 概览卡片 -->
    <div class="section">
      <div class="stats">
        <div class="stat">
          <div class="label">运行中的同步任务</div>
          <div class="value">{{ status?.sync_running_count ?? '—' }}</div>
          <div class="sub">共 {{ status?.sync_tasks_count ?? 0 }} 个任务</div>
        </div>
        <div class="stat">
          <div class="label">备份计划</div>
          <div class="value">{{ status?.backup_plans_count ?? '—' }}</div>
          <div class="sub">最近备份 {{ formatTime(status?.last_backup_time) }}</div>
        </div>
        <div class="stat">
          <div class="label">数据库</div>
          <div class="value">{{ status?.database_count ?? '—' }}</div>
          <div class="sub">已配置的连接</div>
        </div>
        <div class="stat">
          <div class="label">磁盘占用</div>
          <div class="value" :class="diskClass()">
            {{ status?.disk_usage?.percent ? diskPercent() + '%' : '—' }}
          </div>
          <div class="sub">
            <template v-if="status?.disk_usage?.free">
              剩余 {{ formatSize(status.disk_usage.free) }}
            </template>
            <template v-else>不可用</template>
          </div>
        </div>
      </div>
    </div>

    <!-- 同步任务 -->
    <div class="section">
      <div class="section-head">
        <h2>同步任务</h2>
        <button class="btn btn-outline btn-sm" type="button" @click="load">
          <Icon name="refresh" :size="14" />
          刷新
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
              <th>未应用</th>
              <th>最后同步</th>
            </tr>
          </thead>
          <tbody>
            <TableState :cols="7" :loading="loading" :count="syncTasks.length" empty-text="暂无同步任务" />
            <tr v-for="task in syncTasks" :key="task.id">
              <td><strong>{{ task.name }}</strong></td>
              <td class="small">
                {{ task.source_db_name || '?' }} → {{ task.target_db_name || '?' }}
              </td>
              <td>
                <Badge :kind="syncStatusKind(task.status)" dot :pulse="task.running">
                  {{ syncStatusText(task.status) }}
                </Badge>
              </td>
              <td>
                <Badge :kind="healthKind(task.health)">
                  {{ syncHealthText(task.health) }}
                </Badge>
              </td>
              <td class="cell-mono">{{ task.sync_delay ? task.sync_delay + ' ms' : '—' }}</td>
              <td>
                <span :class="task.dlq_count ? 'text-warning' : 'muted'">{{ task.dlq_count }}</span>
              </td>
              <td class="small muted">{{ formatTime(task.last_sync_time) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 最近备份 -->
    <div class="section">
      <div class="section-head">
        <h2>最近备份</h2>
        <button class="btn btn-outline btn-sm" type="button" @click="router.push({ name: 'backups' })">
          查看全部
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
            </tr>
          </thead>
          <tbody>
            <TableState :cols="5" :loading="loading" :count="recent.length" empty-text="暂无备份记录" />
            <tr v-for="item in recent" :key="item.id">
              <td class="small">{{ formatTime(item.created_at) }}</td>
              <td>{{ item.plan_name || `#${item.backup_plan_id}` }}</td>
              <td>
                <Badge :kind="backupStatusKind(item.status)">
                  {{ backupStatusText(item.status) }}
                </Badge>
              </td>
              <td class="cell-mono">{{ formatSize(item.file_size) }}</td>
              <td class="cell-mono">{{ item.duration != null ? item.duration + 's' : '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 运行环境 -->
    <div v-if="status" class="section">
      <h2 style="margin-bottom: 14px">运行环境</h2>
      <div class="card">
        <dl class="kv">
          <dt>服务版本</dt>
          <dd>{{ status.app_name }} v{{ status.app_version }}</dd>
          <dt>进程运行</dt>
          <dd>{{ formatUptime(status.uptime_seconds) }}</dd>
          <dt>系统运行</dt>
          <dd>{{ formatUptime(status.system_uptime_seconds) }}</dd>
          <dt>备份目录</dt>
          <dd class="mono">{{ status.backup_dir }}</dd>
          <dt>mysqldump</dt>
          <dd>
            <span v-if="status.mysql_tools?.mysqldump" class="mono text-success">
              {{ status.mysql_tools.mysqldump }}
            </span>
            <span v-else class="text-danger">未检测到，备份功能不可用</span>
          </dd>
          <dt>mysql</dt>
          <dd>
            <span v-if="status.mysql_tools?.mysql" class="mono text-success">
              {{ status.mysql_tools.mysql }}
            </span>
            <span v-else class="text-danger">未检测到，恢复/复制功能不可用</span>
          </dd>
        </dl>
      </div>
    </div>
  </div>
</template>
