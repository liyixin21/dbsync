<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Icon from '@/components/Icon.vue'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const ui = useUiStore()

const username = ref('')
const password = ref('')
const errorMessage = ref('')
const busy = ref(false)
const usernameInput = ref<HTMLInputElement | null>(null)

/**
 * 会话过期的行内说明。
 *
 * 横幅消失后才显示，两者不重叠——横幅负责第一眼抓住注意力，
 * 这条负责在横幅淡出后留下可回看的痕迹。
 */
const showExpiredHint = computed(() => ui.sessionExpired && !ui.sessionBannerVisible)

onMounted(async () => {
  await nextTick()
  usernameInput.value?.focus()
})

async function submit(): Promise<void> {
  errorMessage.value = ''

  if (!username.value.trim() || !password.value) {
    errorMessage.value = '请输入用户名和密码'
    return
  }

  busy.value = true
  try {
    await auth.login(username.value.trim(), password.value)
    // 登录成功即清除过期状态，横幅随之消失
    ui.clearSessionExpired()
    await ui.loadTheme()
    ui.success('登录成功')

    const redirect = route.query.redirect
    await router.replace(
      typeof redirect === 'string' && redirect ? redirect : { name: 'dashboard' },
    )
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : '登录失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="auth">
    <div class="auth-card">
      <div class="auth-brand">
        <Icon name="database" :size="34" :stroke="1.5" />
        <h1>DBSync</h1>
        <p>数据库实时同步与全量备份</p>
      </div>

      <!--
        会话过期的行内说明。

        只在右上角横幅消失后才出现，两者不同时显示：
        横幅负责「立刻抓住注意力」，这条负责「横幅消失后留下痕迹」，
        用户回头看到登录页时仍知道发生了什么。
      -->
      <div v-if="showExpiredHint" class="auth-notice">
        <Icon name="shield" :size="15" />
        <span>会话已过期，请重新登录</span>
      </div>

      <form class="auth-form" @submit.prevent="submit">
        <div class="field">
          <label for="login-username">用户名</label>
          <input
            id="login-username"
            ref="usernameInput"
            v-model="username"
            type="text"
            autocomplete="username"
            placeholder="请输入用户名"
            :disabled="busy"
          />
        </div>

        <div class="field">
          <label for="login-password">密码</label>
          <input
            id="login-password"
            v-model="password"
            type="password"
            autocomplete="current-password"
            placeholder="请输入密码"
            :disabled="busy"
          />
        </div>

        <div v-if="errorMessage" class="auth-error">{{ errorMessage }}</div>

        <button class="btn btn-primary" type="submit" :disabled="busy">
          <span v-if="busy" class="spinner" />
          {{ busy ? '登录中…' : '登录' }}
        </button>
      </form>

      <!-- 主题切换放在登录页也应当可用：深色模式是首要需求，不该被挡在登录之后 -->
      <div class="auth-theme">
        <button
          class="auth-theme-btn"
          type="button"
          :class="{ active: ui.mode === 'dark' }"
          title="使用深色主题"
          @click="ui.setMode('dark')"
        >
          <Icon name="moon" :size="15" />
          深色
          <Icon v-if="ui.mode === 'dark'" name="check" :size="13" />
        </button>
        <button
          class="auth-theme-btn"
          type="button"
          :class="{ active: ui.mode === 'light' }"
          title="使用浅色主题"
          @click="ui.setMode('light')"
        >
          <Icon name="sun" :size="15" />
          浅色
          <Icon v-if="ui.mode === 'light'" name="check" :size="13" />
        </button>
        <button
          class="auth-theme-btn"
          type="button"
          :class="{ active: ui.mode === 'system' }"
          title="跟随操作系统外观"
          @click="ui.setMode('system')"
        >
          跟随系统
          <Icon v-if="ui.mode === 'system'" name="check" :size="13" />
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.auth-notice {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 12px;
  margin-bottom: 16px;
  font-size: 13px;
  color: var(--danger);
  background: var(--danger-subtle);
  border: 1px solid color-mix(in srgb, var(--danger) 35%, transparent);
  border-radius: var(--radius);
  animation: notice-in 0.2s ease-out;
}

@keyframes notice-in {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

.auth-theme {
  display: flex;
  justify-content: center;
  gap: 8px;
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
}

.auth-theme-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 10px;
  font-family: inherit;
  font-size: 12px;
  color: var(--fg-muted);
  background: transparent;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
  transition: color 0.12s, border-color 0.12s, background 0.12s;
}
.auth-theme-btn:hover {
  color: var(--fg);
  border-color: var(--fg-muted);
}
.auth-theme-btn.active {
  color: var(--primary);
  background: var(--primary-subtle);
  border-color: color-mix(in srgb, var(--primary) 40%, transparent);
}
</style>
