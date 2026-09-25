<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    kind?: 'neutral' | 'primary' | 'success' | 'danger' | 'warning'
    dot?: boolean
    /** 让圆点呼吸，用于表达「正在运行」 */
    pulse?: boolean
  }>(),
  { kind: 'neutral', dot: false, pulse: false },
)

const classes = computed(() => ['badge', `badge-${props.kind}`])
</script>

<template>
  <span :class="classes">
    <span v-if="dot" class="dot" :class="{ pulse }" />
    <slot />
  </span>
</template>
