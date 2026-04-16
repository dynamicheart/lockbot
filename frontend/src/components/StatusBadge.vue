<template>
  <el-tag
    :type="tagType"
    :effect="effect"
    size="small"
    class="status-badge"
    :class="{ 'is-running': status === 'running' }"
  >
    <span v-if="status === 'running'" class="pulse-dot"></span>
    {{ $t(`status.${status}`) }}
  </el-tag>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  status: { type: String, required: true },
  effect: { type: String, default: 'light' },
})

const tagType = computed(() => {
  const map = {
    running: 'success',
    stopped: 'info',
    error: 'danger',
    inactive: 'warning',
  }
  return map[props.status] || 'info'
})
</script>

<style scoped>
.status-badge {
  gap: 4px;
}
.pulse-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--lb-color-success);
  animation: pulse 1.5s ease-in-out infinite;
  margin-right: 2px;
}
@keyframes pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.4;
    transform: scale(0.75);
  }
}
</style>
