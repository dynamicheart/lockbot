<template>
  <section v-if="source" class="utilization-toolbar">
    <span>{{ $t('utilization.title') }} · {{ config?.name || source.name || 'Prometheus' }}</span>
    <span v-if="state.error" class="source-error">{{
      $t('utilization.errors.' + state.error)
    }}</span>
    <span v-else-if="state.loading">{{ $t('common.loading') }}</span>
    <el-button text size="small" :disabled="state.loading || !config" @click="poller.refresh">{{
      $t('common.refresh')
    }}</el-button>
    <el-button
      v-if="config && state.error === 'auth'"
      text
      size="small"
      @click="authentication = true"
      >{{ $t('utilization.authenticate') }}</el-button
    >
    <details
      v-if="deviceResources && state.snapshot?.hosts?.length && !state.error"
      class="host-panel"
    >
      <summary>{{ $t('utilization.hostStatus') }}</summary>
      <div v-for="node in nodes" :key="node" class="host-summary">
        <strong>{{ node }}</strong>
        <template v-if="hostFor(node)">
          <span>CPU {{ hostValue(hostFor(node).cpu, true) }}</span>
          <span
            >{{ $t('utilization.hostMemory') }} {{ hostValue(hostFor(node).used) }} /
            {{ hostValue(hostFor(node).total) }}</span
          >
          <span v-for="disk in hostFor(node).filesystems" :key="disk.name"
            >{{ $t('utilization.disk') }} {{ disk.name }} {{ hostValue(disk.used) }} /
            {{ hostValue(disk.total) }}</span
          >
        </template>
        <span v-else>—</span>
      </div>
    </details>
    <el-dialog
      v-model="authentication"
      :title="$t('utilization.authenticate')"
      width="440px"
      class="utilization-dialog"
    >
      <p>{{ $t('utilization.sourceToken') }}</p>
      <el-input v-model="bearer" type="password" show-password autocomplete="off" />
      <template #footer
        ><el-button type="primary" @click="applyToken">{{
          $t('common.confirm')
        }}</el-button></template
      >
    </el-dialog>
  </section>
</template>
<script setup>
import { reactive, ref, watch, onBeforeUnmount } from 'vue'
import {
  validateConfig,
  prometheusDatasource,
  resolveHost,
  metricStatus,
} from '../utils/utilization/prometheus'
import { useI18n } from 'vue-i18n'
import { createMetricsPoller } from '../utils/utilization/poller'
const props = defineProps({
  botId: { type: [String, Number], required: true },
  source: { type: Object, default: null },
  nodes: { type: Array, default: () => [] },
  allowed: Boolean,
  deviceResources: Boolean,
})
const emit = defineEmits(['change'])
const { t } = useI18n()
const hostFor = (node) => resolveHost(state.snapshot, config.value, node)
function hostValue(metric, percent = false) {
  const status = metricStatus(metric, Date.now(), config.value?.staleSeconds || 660)
  if (status !== 'ok')
    return status === 'stale'
      ? t('utilization.stale')
      : status === 'failed'
        ? t('utilization.unavailable')
        : '—'
  return percent ? `${Math.round(metric.value)}%` : `${(metric.value / 1024 ** 3).toFixed(1)} GiB`
}
const config = ref(null)
const bearer = ref('')
const authentication = ref(false)
let activeBearer = ''
const state = reactive({ loading: false, snapshot: null, error: null })
function publish(update = {}) {
  Object.assign(state, update)
  emit('change', { ...state, enabled: !!config.value && props.allowed, config: config.value })
}
const poller = createMetricsPoller({
  collect: (signal) =>
    prometheusDatasource.query(config.value, props.nodes, { signal, bearer: activeBearer }),
  publish,
})
function sync() {
  if (config.value && props.allowed && !document.hidden) poller.start(config.value.intervalSeconds)
  else poller.stop()
  publish()
}
watch(
  () => JSON.stringify([props.botId, props.source, props.nodes]),
  () => {
    poller.stop()
    activeBearer = ''
    bearer.value = ''
    authentication.value = false
    state.snapshot = null
    state.error = null
    try {
      config.value = props.source ? validateConfig(props.source) : null
    } catch (error) {
      config.value = null
      state.error = error.code || 'configuration'
    }
    sync()
  },
  { immediate: true }
)
watch(
  () => props.allowed,
  (value) => {
    if (!value) {
      activeBearer = ''
      bearer.value = ''
      authentication.value = false
      state.snapshot = null
    }
    sync()
  }
)
function applyToken() {
  activeBearer = bearer.value
  bearer.value = ''
  authentication.value = false
  // Respect the same polling budget when retrying with a source credential.
  poller.refresh()
}
watch(authentication, (value) => {
  if (!value) bearer.value = ''
})
document.addEventListener('visibilitychange', sync)
onBeforeUnmount(() => {
  poller.stop()
  activeBearer = ''
  bearer.value = ''
  document.removeEventListener('visibilitychange', sync)
})
</script>
<style scoped>
.utilization-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 14px;
  color: var(--lb-text-secondary);
  font-size: 12px;
}
.host-panel {
  width: 100%;
}
.host-panel summary {
  cursor: pointer;
  padding: 6px 0;
}
.host-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  padding: 6px 0;
}
.source-error {
  color: var(--el-color-warning-dark-2);
}
</style>
<style>
.utilization-dialog {
  max-width: calc(100vw - 24px);
}
</style>
