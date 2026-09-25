<script setup lang="ts">
/**
 * 空状态与加载占位。
 */
withDefaults(
  defineProps<{
    /** 表格列数，用于无数据行的跨列 */
    cols?: number
    loading?: boolean
    emptyText?: string
    count?: number
  }>(),
  { cols: 1, loading: false, emptyText: '暂无数据', count: undefined },
)
</script>

<template>
  <tr v-if="loading">
    <td :colspan="cols">
      <div class="loading-block">
        <span class="spinner" />
        加载中…
      </div>
    </td>
  </tr>
  <tr v-else-if="count === 0">
    <td :colspan="cols">
      <div class="empty">{{ emptyText }}</div>
    </td>
  </tr>
</template>
