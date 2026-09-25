<script setup lang="ts">
import Icon from './Icon.vue'

const props = withDefaults(
  defineProps<{
    total: number
    skip: number
    limit: number
  }>(),
  {},
)

const emit = defineEmits<{ change: [skip: number] }>()

const totalPages = () => Math.max(1, Math.ceil(props.total / props.limit))
const currentPage = () => Math.floor(props.skip / props.limit) + 1

function go(delta: number): void {
  const next = props.skip + delta * props.limit
  if (next < 0 || next >= props.total) return
  emit('change', next)
}
</script>

<template>
  <div v-if="total > limit" class="pager">
    <button
      class="btn btn-outline btn-sm"
      type="button"
      :disabled="skip <= 0"
      @click="go(-1)"
    >
      <Icon name="chevronLeft" :size="14" />
      上一页
    </button>
    <span>第 {{ currentPage() }} / {{ totalPages() }} 页 · 共 {{ total }} 条</span>
    <button
      class="btn btn-outline btn-sm"
      type="button"
      :disabled="skip + limit >= total"
      @click="go(1)"
    >
      下一页
      <Icon name="chevronRight" :size="14" />
    </button>
  </div>
</template>
