<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import Icon from '@/components/Icon.vue'
import ThemePicker from '@/components/ThemePicker.vue'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const ui = useUiStore()

const drawerOpen = ref(false)

const NAV = [
  { name: 'dashboard', label: '概览', icon: 'dashboard' },
  { name: 'databases', label: '数据库管理', icon: 'database' },
  { name: 'sync', label: '同步管理', icon: 'sync' },
  { name: 'backups', label: '备份管理', icon: 'backup' },
  { name: 'logs', label: '日志管理', icon: 'logs' },
  { name: 'settings', label: '设置', icon: 'settings' },
]

const pageTitle = computed(() => (route.meta.title as string) || 'DBSync')

function closeDrawer(): void {
  drawerOpen.value = false
}

function logout(): void {
  auth.logout()
  // 主动退出是正常操作，必须清掉可能残留的过期标志，
  // 否则会同时看到「已退出登录」和「登录已过期」两条信息。
  ui.clearSessionExpired()
  ui.info('已退出登录')
  void router.replace({ name: 'login' })
}
</script>

<template>
  <div class="shell">
    <div class="nav-scrim" :class="{ show: drawerOpen }" @click="closeDrawer" />

    <aside class="sidebar" :class="{ open: drawerOpen }">
      <div class="brand">
        <Icon name="database" :size="20" />
        DBSync
      </div>

      <nav class="nav">
        <RouterLink
          v-for="item in NAV"
          :key="item.name"
          :to="{ name: item.name }"
          :class="{ active: route.name === item.name }"
          @click="closeDrawer"
        >
          <Icon :name="item.icon" :size="17" />
          {{ item.label }}
        </RouterLink>
      </nav>

      <div class="sidebar-foot">
        <div>{{ auth.username || '未登录' }}</div>
        <div class="small">v2.0.0</div>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <button
          class="icon-btn nav-toggle"
          type="button"
          aria-label="菜单"
          @click="drawerOpen = true"
        >
          <Icon name="menu" />
        </button>

        <h1>{{ pageTitle }}</h1>

        <ThemePicker />

        <button class="icon-btn" type="button" title="退出登录" @click="logout">
          <Icon name="logout" />
        </button>
      </header>

      <main class="content">
        <RouterView />
      </main>
    </div>
  </div>
</template>
