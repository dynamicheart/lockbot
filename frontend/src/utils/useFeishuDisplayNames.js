import { inject, onBeforeUnmount, ref, watch } from 'vue'
import api from './api'

export const actorDisplayKey = Symbol('lockbot.actorDisplay')
const fallback = (id, label) => label || id || '—'

export function useActorDisplay() {
  return inject(actorDisplayKey, { label: fallback })
}

/** Display-only enrichment. No identities, credentials or notes enter these requests. */
export function useFeishuDisplayNames(scope) {
  const names = ref({})
  const status = ref('ready')
  let generation = 0
  let controller = null
  let timer = null
  let disposed = false
  const delays = [2000, 4000, 8000]
  function cancel() {
    generation++
    controller?.abort()
    controller = null
    clearTimeout(timer)
    timer = null
  }
  async function request(method, run, target, page, attempt) {
    if (disposed || run !== generation) return
    const pending = new AbortController()
    controller = pending
    try {
      const path = `/feishu/bots/${target.id}/display-names`
      const options = { signal: pending.signal, params: { log_page: page }, _silent: true }
      const { data } =
        method === 'post' ? await api.post(path, {}, options) : await api.get(path, options)
      if (disposed || run !== generation) return
      const next = {}
      for (const [id, name] of Object.entries(data.names || {})) {
        if (
          /^ou_[A-Za-z0-9_-]+$/.test(id) &&
          typeof name === 'string' &&
          name.trim() &&
          name.length <= 80
        )
          next[id] = name
      }
      names.value = { ...names.value, ...next }
      status.value = data.status || 'unavailable'
      if (status.value === 'pending' && attempt < delays.length) {
        timer = setTimeout(() => {
          timer = null
          request('get', run, target, page, attempt + 1)
        }, delays[attempt])
      } else if (status.value === 'pending') status.value = 'unavailable'
    } catch (error) {
      if (disposed || run !== generation || error?.code === 'ERR_CANCELED') return
      names.value = {}
      status.value = 'unavailable'
    } finally {
      if (run === generation && controller === pending) controller = null
    }
  }
  function refresh(page = 1) {
    cancel()
    if (disposed || !scope.value) return
    status.value = 'pending'
    const selectedPage = Number.isFinite(Number(page)) ? Math.trunc(Number(page)) : 1
    return request(
      'post',
      generation,
      { ...scope.value },
      Math.min(20, Math.max(1, selectedPage)),
      0
    )
  }
  watch(
    () => scope.value?.key || '',
    () => {
      cancel()
      names.value = {}
      status.value = 'ready'
      if (scope.value) refresh()
    },
    { immediate: true, flush: 'sync' }
  )
  onBeforeUnmount(() => {
    disposed = true
    cancel()
    names.value = {}
  })
  return {
    names,
    status,
    refresh,
    label: (id, label) => (Object.hasOwn(names.value, id) ? names.value[id] : fallback(id, label)),
  }
}
