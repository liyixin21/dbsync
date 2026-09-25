<script setup lang="ts">
import Icon from './Icon.vue'

const props = withDefaults(
  defineProps<{
    title: string
    confirmText?: string
    danger?: boolean
    busy?: boolean
  }>(),
  { confirmText: '确认', danger: false, busy: false },
)

const emit = defineEmits<{ close: []; confirm: [] }>()
</script>

<template>
  <div class="modal-backdrop" @click.self="emit('close')" @keydown.esc="emit('close')">
    <div class="modal" role="dialog" aria-modal="true">
      <div class="modal-head">
        <h3>{{ title }}</h3>
        <button class="icon-btn" type="button" aria-label="关闭" @click="emit('close')">
          <Icon name="close" :size="16" />
        </button>
      </div>

      <div class="modal-body">
        <slot />
      </div>

      <div class="modal-foot">
        <slot name="footer">
          <button class="btn btn-ghost" type="button" :disabled="busy" @click="emit('close')">
            取消
          </button>
          <button
            class="btn"
            :class="danger ? 'btn-danger' : 'btn-primary'"
            type="button"
            :disabled="busy"
            @click="emit('confirm')"
          >
            <span v-if="busy" class="spinner" />
            {{ confirmText }}
          </button>
        </slot>
      </div>
    </div>
  </div>
</template>
