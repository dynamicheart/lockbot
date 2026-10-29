import { onMounted, onUnmounted, ref } from 'vue'

// Presentation time stays live even when data collection is paused or unreachable.
// Updating this ref does not initiate a request or hardware sample.
export function useDisplayClock() {
  const now = ref(Date.now())
  let timer
  onMounted(() => {
    now.value = Date.now()
    timer = setInterval(() => (now.value = Date.now()), 1000)
  })
  onUnmounted(() => clearInterval(timer))
  return now
}
