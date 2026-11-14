<template>
  <span class="sr-only">{{ value }}</span>
  <span aria-hidden="true" class="rolling-count">{{ display }}</span>
</template>

<script setup>
import { onUnmounted, ref, watch } from 'vue'

const props = defineProps({ value: { type: Number, required: true } })
const display = ref(props.value)
let frame

function update(value) {
  cancelAnimationFrame(frame)
  const previous = display.value
  if (previous === value || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    display.value = value
    return
  }
  const started = performance.now()
  const draw = (now) => {
    const progress = Math.min(1, Math.max(0, (now - started) / 360))
    display.value = Math.round(previous + (value - previous) * (1 - (1 - progress) ** 3))
    if (progress < 1) frame = requestAnimationFrame(draw)
  }
  frame = requestAnimationFrame(draw)
}
watch(() => props.value, update)
onUnmounted(() => cancelAnimationFrame(frame))
</script>

<style scoped>
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
}
</style>
