<script setup lang="ts">
/**
 * 主题切换控件：浅色 / 深色 / 跟随系统。
 *
 * 旧实现是单按钮二态循环，用户无法回到「跟随系统」，
 * 一旦点过一次就被永久钉死在某一种模式上。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import Icon from '@/components/Icon.vue'
import { useUiStore } from '@/stores/ui'
import type { ThemeMode } from '@/stores/ui'

const ui = useUiStore()
const open = ref(false)
const root = ref<HTMLElement | null>(null)

const OPTIONS: Array<{ value: ThemeMode; label: string; icon: string }> = [
  { value: 'light', label: '浅色', icon: 'sun' },
  { value: 'dark', label: '深色', icon: 'moon' },
  { value: 'system', label: '跟随系统', icon: 'dashboard' },
]

function choose(value: ThemeMode): void {
  ui.setMode(value)
  open.value = false
}

function onDocumentClick(event: MouseEvent): void {
  if (!open.value) return
  if (root.value && !root.value.contains(event.target as Node)) {
    open.value = false
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') open.value = false
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div ref="root" class="theme-picker">
    <button
      class="icon-btn"
      type="button"
      :title="`主题：${OPTIONS.find((o) => o.value === ui.mode)?.label}`"
      aria-haspopup="menu"
      :aria-expanded="open"
      @click="open = !open"
    >
      <Icon :name="ui.dark ? 'moon' : 'sun'" />
    </button>

    <Transition name="menu">
      <div v-if="open" class="theme-menu" role="menu">
        <button
          v-for="option in OPTIONS"
          :key="option.value"
          class="theme-menu-item"
          :class="{ active: ui.mode === option.value }"
          type="button"
          role="menuitem"
          @click="choose(option.value)"
        >
          <Icon :name="option.icon" :size="15" />
          <span class="grow">{{ option.label }}</span>
          <Icon v-if="ui.mode === option.value" name="check" :size="14" />
        </button>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.theme-picker {
  position: relative;
}

.theme-menu {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 50;
  min-width: 150px;
  padding: 4px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
}

.theme-menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 9px;
  font-family: inherit;
  font-size: 13px;
  color: var(--fg);
  text-align: left;
  background: transparent;
  border: none;
  border-radius: var(--radius);
  cursor: pointer;
}
.theme-menu-item:hover {
  background: var(--bg-subtle);
}
.theme-menu-item.active {
  color: var(--primary);
  background: var(--primary-subtle);
}

.menu-enter-active,
.menu-leave-active {
  transition: opacity 0.12s ease, transform 0.12s ease;
}
.menu-enter-from,
.menu-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
