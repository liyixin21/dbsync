<script setup lang="ts">
/**
 * 应用根组件：全局浮层容器。
 *
 * 所有提示统一在右上角纵向堆叠，会话横幅排在最上方。
 * 不使用左下角/右下角：单一位置能让用户形成稳定预期，不必四处找提示。
 */
import Icon from '@/components/Icon.vue'
import SessionExpiredBanner from '@/components/SessionExpiredBanner.vue'
import { useUiStore } from '@/stores/ui'

const ui = useUiStore()
</script>

<template>
  <router-view />

  <div class="notice-stack">
    <!-- 会话过期提示：与普通提示同款式，3 秒后自动淡出 -->
    <SessionExpiredBanner />

    <!-- 普通提示：在会话提示下方依次堆叠 -->
    <div
      v-for="t in ui.toasts"
      :key="t.id"
      class="toast"
      :class="t.kind"
      role="status"
    >
      <Icon
        :name="t.kind === 'success' ? 'check' : t.kind === 'error' ? 'alert' : 'info'"
        :size="15"
      />
      <span class="toast-text">{{ t.message }}</span>
      <button class="close" type="button" aria-label="关闭" @click="ui.dismissToast(t.id)">
        ×
      </button>
    </div>
  </div>

  <!-- 全屏加载 -->
  <div v-if="ui.loading" class="overlay-loading">
    <div class="box">
      <span class="spinner lg" />
      <span>{{ ui.loading }}</span>
    </div>
  </div>
</template>
