<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Badge from '@/components/Badge.vue'
import Icon from '@/components/Icon.vue'
import Modal from '@/components/Modal.vue'
import TableState from '@/components/TableState.vue'
import { api } from '@/api/client'
import type { DatabaseConfig, DatabasePayload } from '@/api/types'
import { formatTime } from '@/utils/format'
import { useUiStore } from '@/stores/ui'

const ui = useUiStore()

const items = ref<DatabaseConfig[]>([])
const loading = ref(true)
const search = ref('')

const dialogOpen = ref(false)
const editingId = ref<number | null>(null)
const saving = ref(false)
const testing = ref(false)
const connectionOk = ref(false)

const form = ref<DatabasePayload & { password: string }>({
  name: '',
  host: '',
  port: 3306,
  username: '',
  password: '',
  database_name: '',
  is_active: true,
})

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return items.value
  return items.value.filter((d) =>
    [d.name, d.host, d.database_name, d.username].some((v) =>
      (v || '').toLowerCase().includes(q),
    ),
  )
})

async function load(): Promise<void> {
  loading.value = true
  try {
    items.value = await api.listDatabases()
  } catch (err) {
    ui.fail(err, '加载数据库列表失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)

function openCreate(): void {
  editingId.value = null
  connectionOk.value = false
  form.value = {
    name: '',
    host: '',
    port: 3306,
    username: '',
    password: '',
    database_name: '',
    is_active: true,
  }
  dialogOpen.value = true
}

function openEdit(record: DatabaseConfig): void {
  editingId.value = record.id
  // 编辑时不回填密码；留空表示保持原密码
  connectionOk.value = false
  form.value = {
    name: record.name,
    host: record.host,
    port: record.port,
    username: record.username,
    password: '',
    database_name: record.database_name,
    is_active: record.is_active,
  }
  dialogOpen.value = true
}

/**
 * 连接测试。
 *
 * 编辑已有记录且未改密码时，后端会用已保存的凭据测试，
 * 因此不需要用户为了测试而重新输入密码。
 */
async function testConnection(): Promise<void> {
  if (!form.value.host || !form.value.username || !form.value.database_name) {
    ui.warn('请先填写主机、用户名和数据库名')
    return
  }
  if (!form.value.password && editingId.value === null) {
    ui.warn('请输入密码')
    return
  }

  testing.value = true
  try {
    const result = await api.testConnection({
      ...form.value,
      database_id: editingId.value ?? undefined,
    })
    connectionOk.value = result.success
    if (result.success) ui.success(result.message)
    else ui.error(result.message)
  } catch (err) {
    connectionOk.value = false
    ui.fail(err, '连接测试失败')
  } finally {
    testing.value = false
  }
}

async function save(): Promise<void> {
  if (!form.value.name || !form.value.host || !form.value.username || !form.value.database_name) {
    ui.warn('请填写所有必填字段')
    return
  }
  if (editingId.value === null && !form.value.password) {
    ui.warn('请输入密码')
    return
  }

  saving.value = true
  try {
    if (editingId.value !== null) {
      const payload: Partial<DatabasePayload> = { ...form.value }
      if (!payload.password) delete payload.password
      await api.updateDatabase(editingId.value, payload)
      ui.success('数据库已更新')
    } else {
      await api.createDatabase(form.value)
      ui.success('数据库已添加')
    }
    dialogOpen.value = false
    await load()
  } catch (err) {
    ui.fail(err, '保存失败')
  } finally {
    saving.value = false
  }
}

async function remove(record: DatabaseConfig): Promise<void> {
  if (!window.confirm(`确定删除数据库配置「${record.name}」吗？`)) return
  try {
    await api.deleteDatabase(record.id)
    ui.success('已删除')
    await load()
  } catch (err) {
    // 被同步任务或备份计划引用时后端会给出明确原因
    ui.fail(err, '删除失败')
  }
}

function resetConnectionFlag(): void {
  connectionOk.value = false
}
</script>

<template>
  <div>
    <div class="section-head">
      <div class="toolbar grow">
        <div style="position: relative">
          <input
            v-model="search"
            type="search"
            placeholder="搜索名称、主机或库名…"
            style="width: 240px"
          />
        </div>
      </div>
      <button class="btn btn-primary" type="button" @click="openCreate">
        <Icon name="plus" :size="15" />
        添加数据库
      </button>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>地址</th>
            <th>数据库</th>
            <th>用户名</th>
            <th>状态</th>
            <th>创建时间</th>
            <th style="width: 90px"></th>
          </tr>
        </thead>
        <tbody>
          <TableState
            :cols="7"
            :loading="loading"
            :count="filtered.length"
            empty-text="暂无数据库配置，点击右上角添加"
          />
          <tr v-for="record in filtered" :key="record.id">
            <td>
              <strong>{{ record.name }}</strong>
              <div v-if="!record.password_set" class="small text-warning">未设置密码</div>
            </td>
            <td class="mono small">{{ record.host }}:{{ record.port }}</td>
            <td class="mono">{{ record.database_name }}</td>
            <td class="small">{{ record.username }}</td>
            <td>
              <Badge :kind="record.is_active ? 'success' : 'neutral'">
                {{ record.is_active ? '启用' : '禁用' }}
              </Badge>
            </td>
            <td class="small muted">{{ formatTime(record.created_at) }}</td>
            <td class="actions">
              <button class="icon-btn" type="button" title="编辑" @click="openEdit(record)">
                <Icon name="edit" :size="15" />
              </button>
              <button
                class="icon-btn is-danger"
                type="button"
                title="删除"
                @click="remove(record)"
              >
                <Icon name="trash" :size="15" />
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 新增/编辑对话框 -->
    <Modal
      v-if="dialogOpen"
      :title="editingId !== null ? '编辑数据库' : '添加数据库'"
      :confirm-text="editingId !== null ? '保存' : '添加'"
      :busy="saving"
      @close="dialogOpen = false"
      @confirm="save"
    >
      <div class="field">
        <label>显示名称 *</label>
        <input v-model="form.name" type="text" placeholder="例如：生产主库" @input="resetConnectionFlag" />
      </div>

      <div class="row-2">
        <div class="field">
          <label>主机地址 *</label>
          <input v-model="form.host" type="text" placeholder="127.0.0.1" @input="resetConnectionFlag" />
        </div>
        <div class="field">
          <label>端口 *</label>
          <input v-model.number="form.port" type="number" min="1" max="65535" @input="resetConnectionFlag" />
        </div>
      </div>

      <div class="row-2">
        <div class="field">
          <label>用户名 *</label>
          <input v-model="form.username" type="text" @input="resetConnectionFlag" />
        </div>
        <div class="field">
          <label>
            密码
            <template v-if="editingId !== null">（留空则不修改）</template>
          </label>
          <input
            v-model="form.password"
            type="password"
            autocomplete="new-password"
            @input="resetConnectionFlag"
          />
        </div>
      </div>

      <div class="field">
        <label>数据库名 *</label>
        <input v-model="form.database_name" type="text" @input="resetConnectionFlag" />
      </div>

      <div class="row">
        <label class="switch">
          <input v-model="form.is_active" type="checkbox" />
          <span class="track" />
        </label>
        <span>启用该数据库配置</span>
      </div>

      <div v-if="connectionOk" class="row small text-success">
        <Icon name="check" :size="14" />
        连接已验证
      </div>

      <template #footer>
        <button class="btn btn-ghost" type="button" :disabled="saving" @click="dialogOpen = false">
          取消
        </button>
        <button class="btn btn-outline" type="button" :disabled="testing || saving" @click="testConnection">
          <span v-if="testing" class="spinner" />
          {{ testing ? '测试中…' : '测试连接' }}
        </button>
        <button class="btn btn-primary" type="button" :disabled="saving || testing" @click="save">
          <span v-if="saving" class="spinner" />
          {{ editingId !== null ? '保存' : '添加' }}
        </button>
      </template>
    </Modal>
  </div>
</template>
