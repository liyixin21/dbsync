<script setup lang="ts">
/**
 * 会话过期提示。
 *
 * 外观与普通提示（toast）保持一致，不做特殊化处理：
 * 白底、左侧色条、单行文案，只是色条用警示色以体现严重程度。
 *
 * 行为：
 * - 出现后 3 秒自动淡出
 * - 淡出后登录页输入框上方留下静态说明，用户仍知道为何回到登录页
 * - 也可手动点 × 提前关闭
 *
 * 不提供「立即登录」按钮：出现该提示时路由已跳转到登录页，按钮没有实际作用。
 */
import { onBeforeUnmount, ref, watch } from 'vue'
import Icon from '@/components/Icon.vue'
import { useUiStore } from '@/stores/ui'

const ui = useUiStore()

/** 自动消失延时（毫秒）。 */
const AUTO_DISMISS_MS = 3000

const visible = ref(false)
let timer: number | null = null

// 同步到全局状态：登录页据此决定是否显示行内说明
watch(
  visible,
  (v) => {
    ui.sessionBannerVisible = v
  },
  { flush: 'sync' },
)

function clearTimer(): void {
  if (timer !== null) {
    window.clearTimeout(timer)
    timer = null
  }
}

/** 会话过期状态出现时展示，并启动自动消失。 */
watch(
  () => ui.sessionExpired,
  (expired) => {
    clearTimer()
    if (!expired) {
      visible.value = false
      return
    }
    visible.value = true
    timer = window.setTimeout(() => {
      visible.value = false
      timer = null
    }, AUTO_DISMISS_MS)
  },
  { immediate: true },
)

function dismiss(): void {
  clearTimer()
  visible.value = false
}

onBeforeUnmount(() => {
  clearTimer()
  ui.sessionBannerVisible = false
})
</script>

<template>
  <Transition name="banner">
    <div v-if="visible" class="toast error session-notice" role="alert">
      <Icon name="alert" :size="15" />
      <span class="toast-text">登录已过期，请重新登录</span>
      <button class="close" type="button" aria-label="关闭" @click="dismiss">×</button>
    </div>
  </Transition>
</template>

<style scoped>
/* 会话提示复用 .toast 的外观，仅保留组件级微调。
   配色与尺寸全部来自 tokens.css 的 .toast 规则，
   避免出现「另一种提示样式」。 */
.session-notice {
  /* 覆盖 .toast 的进入动画：从上方滑入更贴合右上角位置 */
  animation: notice-in 0.18s cubic-bezier(0.2, 0.9, 0.3, 1);
}

@keyframes notice-in {
  from {
    opacity: 0;
    transform: translateY(-8px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

/* 出现/淡出过渡 */
.banner-enter-active,
.banner-leave-active {
  transition: opacity 0.3s ease, transform 0.3s ease;
}

.banner-enter-from,
.banner-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}
</style>
