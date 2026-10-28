<template>
  <section class="utilization-control">
    <div class="utilization-toolbar">
      <el-checkbox v-model="enabled" :disabled="!config || !allowed">{{
        $t('utilization.show')
      }}</el-checkbox>
      <el-button text size="small" @click="openSettings">{{
        $t('utilization.configure')
      }}</el-button>
      <template v-if="enabled">
        <span class="source-name">{{ config.name }}</span>
        <span v-if="state.error" class="source-error">{{
          $t('utilization.errors.' + state.error)
        }}</span>
        <span v-else-if="state.loading" class="source-name">{{ $t('common.loading') }}</span>
        <el-button text size="small" :disabled="state.loading" @click="poller.refresh">{{
          $t('common.refresh')
        }}</el-button>
      </template>
    </div>
    <el-dialog
      v-model="settings"
      :title="$t('utilization.configure')"
      width="560px"
      class="utilization-dialog"
    >
      <el-form label-position="top" @submit.prevent="testAndEnable">
        <el-form-item :label="$t('utilization.name')"
          ><el-input v-model="draft.name" maxlength="64"
        /></el-form-item>
        <el-form-item :label="$t('utilization.endpoint')"
          ><el-input v-model="draft.endpoint" placeholder="https://prometheus.example.com"
        /></el-form-item>
        <el-form-item :label="$t('utilization.sourceToken')"
          ><el-input v-model="bearer" type="password" show-password autocomplete="off"
        /></el-form-item>
        <div class="mapping-row">
          <el-form-item :label="$t('utilization.nodeLabel')"
            ><el-input v-model="draft.nodeLabel"
          /></el-form-item>
          <el-form-item :label="$t('utilization.deviceLabel')"
            ><el-input v-model="draft.deviceLabel"
          /></el-form-item>
        </div>
        <el-form-item :label="$t('utilization.nodeMapping')"
          ><el-input v-model="nodeMappingText" type="textarea" :rows="2"
        /></el-form-item>
        <el-form-item v-if="deviceResources" :label="$t('utilization.deviceMapping')"
          ><el-input v-model="deviceMappingText" type="textarea" :rows="3"
        /></el-form-item>
        <el-collapse>
          <el-collapse-item :title="$t('utilization.metrics')" name="metrics">
            <el-form-item :label="$t('utilization.computeMetric')"
              ><el-input v-model="draft.utilizationMetric"
            /></el-form-item>
            <el-form-item :label="$t('utilization.usedMetric')"
              ><el-input v-model="draft.memoryUsedMetric"
            /></el-form-item>
            <el-form-item :label="$t('utilization.totalMetric')"
              ><el-input v-model="draft.memoryTotalMetric"
            /></el-form-item>
            <div class="mapping-row">
              <el-form-item :label="$t('utilization.memoryUnit')"
                ><el-select v-model="draft.memoryUnit"
                  ><el-option value="MiB" label="MiB" /><el-option
                    value="B"
                    label="Bytes" /></el-select
              ></el-form-item>
              <el-form-item :label="$t('utilization.interval')"
                ><el-input-number v-model="draft.intervalSeconds" :min="30" :max="3600"
              /></el-form-item>
            </div>
          </el-collapse-item>
        </el-collapse>
        <p class="config-note">{{ $t('utilization.boundary') }}</p>
        <p v-if="configError" class="source-error">{{ $t('utilization.errors.' + configError) }}</p>
      </el-form>
      <template #footer>
        <el-button @click="settings = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="testing" :disabled="!allowed" @click="testAndEnable">{{
          $t('utilization.testEnable')
        }}</el-button>
      </template>
    </el-dialog>
  </section>
</template>
<script setup>
import { reactive, ref, watch, onBeforeUnmount } from 'vue'
import {
  DEFAULT_CONFIG,
  validateConfig,
  configStorageKey,
  prometheusDatasource,
  resolveCards,
  ProviderError,
} from '../utils/utilization/prometheus'
import { createMetricsPoller } from '../utils/utilization/poller'
const props = defineProps({
  botId: { type: [String, Number], required: true },
  nodes: { type: Array, default: () => [] },
  resources: { type: Array, default: () => [] },
  allowed: Boolean,
  deviceResources: Boolean,
})
const emit = defineEmits(['change'])
const enabled = ref(false)
const config = ref(null)
const settings = ref(false)
const draft = reactive({ ...DEFAULT_CONFIG })
const nodeMappingText = ref('{}')
const deviceMappingText = ref('{}')
const bearer = ref('')
let activeBearer = ''
let nextTestAt = 0
const testing = ref(false)
const configError = ref(null)
const state = reactive({ loading: false, snapshot: null, error: null })
let storageKey = ''
let scope = 0
let testController
let discoveryController
function publish(update = {}) {
  Object.assign(state, update)
  emit('change', { ...state, enabled: enabled.value && props.allowed, config: config.value })
}
const poller = createMetricsPoller({
  collect: (signal) =>
    prometheusDatasource.query(config.value, props.nodes, { signal, bearer: activeBearer }),
  publish,
})
function sync() {
  if (enabled.value && props.allowed && config.value && !document.hidden && !testing.value)
    poller.start(config.value.intervalSeconds)
  else poller.stop()
  publish()
}
watch([enabled, () => props.allowed], () => {
  if (!enabled.value) {
    state.snapshot = null
    state.error = null
  }
  if (!props.allowed) {
    enabled.value = false
    testController?.abort()
    settings.value = false
    bearer.value = ''
    activeBearer = ''
    state.snapshot = null
  }
  sync()
})
watch(
  () => String(props.botId),
  async () => {
    scope++
    const run = scope
    enabled.value = false
    config.value = null
    state.snapshot = null
    state.error = null
    bearer.value = ''
    activeBearer = ''
    testing.value = false
    storageKey = ''
    testController?.abort()
    discoveryController?.abort()
    discoveryController = new AbortController()
    poller.stop()
    publish()
    try {
      // Existing public identity endpoint; no management credentials or view code.
      const response = await fetch('/api/agent-skill', {
        credentials: 'omit',
        redirect: 'error',
        cache: 'no-store',
        signal: discoveryController.signal,
      })
      if (!response.ok) return
      const manifest = await response.json()
      if (run !== scope || typeof manifest.instance_id !== 'string') return
      storageKey = configStorageKey(location.origin, manifest.instance_id, props.botId)
      const saved = localStorage.getItem(storageKey)
      if (saved) config.value = validateConfig(JSON.parse(saved))
    } catch {
      // Configuration persistence is optional. Never guess a shared-instance namespace.
    }
    publish()
  },
  { immediate: true }
)
function openSettings() {
  bearer.value = ''
  Object.assign(
    draft,
    config.value || {
      ...DEFAULT_CONFIG,
      id: [...crypto.getRandomValues(new Uint8Array(12))]
        .map((byte) => byte.toString(16).padStart(2, '0'))
        .join(''),
    }
  )
  nodeMappingText.value = JSON.stringify(
    config.value?.nodeMapping || Object.fromEntries(props.nodes.map((node) => [node, node])),
    null,
    2
  )
  deviceMappingText.value = JSON.stringify(
    config.value?.deviceMapping ||
      Object.fromEntries(props.resources.map((id) => [id, '<device UUID>'])),
    null,
    2
  )
  configError.value = null
  settings.value = true
}
async function testAndEnable() {
  if (testing.value || !props.allowed || document.hidden) return
  if (performance.now() < nextTestAt) {
    configError.value = 'limited'
    return
  }
  configError.value = null
  let candidate
  try {
    candidate = validateConfig({
      ...draft,
      nodeMapping: JSON.parse(nodeMappingText.value),
      deviceMapping: JSON.parse(deviceMappingText.value),
    })
  } catch (error) {
    configError.value = error.code || 'configuration'
    return
  }
  const run = scope
  nextTestAt = performance.now() + Math.max(30, candidate.intervalSeconds) * 1000
  testing.value = true
  poller.stop()
  testController?.abort()
  const currentController = new AbortController()
  testController = currentController
  const timeout = setTimeout(() => currentController.abort(), 15000)
  try {
    const snapshot = await prometheusDatasource.testDatasource(candidate, props.nodes, {
      signal: currentController.signal,
      bearer: bearer.value,
    })
    if (run !== scope || !props.allowed || currentController.signal.aborted) return
    if (
      props.deviceResources &&
      !props.resources.some(
        (id) => resolveCards(snapshot, candidate, id.replace(/:dev[0-9]+$/, ''), id).length
      )
    )
      throw new ProviderError('mapping')
    config.value = candidate
    activeBearer = bearer.value
    bearer.value = ''
    if (storageKey) {
      try {
        localStorage.setItem(storageKey, JSON.stringify(candidate))
      } catch {
        /* Optional browser-local settings. */
      }
    }
    state.snapshot = snapshot
    state.error = null
    settings.value = false
    enabled.value = true
  } catch (error) {
    if (run === scope && props.allowed)
      configError.value = currentController.signal.aborted ? 'timeout' : error.code || 'network'
  } finally {
    clearTimeout(timeout)
    currentController.abort()
    if (run === scope) {
      testing.value = false
      poller.deferUntil(nextTestAt)
      sync()
    }
  }
}
watch(settings, (value) => {
  if (!value) {
    testController?.abort()
    bearer.value = ''
  }
})
const visibility = () => {
  if (document.hidden) testController?.abort()
  sync()
}
document.addEventListener('visibilitychange', visibility)
onBeforeUnmount(() => {
  scope++
  poller.stop()
  testController?.abort()
  discoveryController?.abort()
  document.removeEventListener('visibilitychange', visibility)
  bearer.value = ''
  activeBearer = ''
})
</script>
<style scoped>
.utilization-control {
  margin: 0 0 14px;
}
.utilization-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 12px;
}
.utilization-toolbar :deep(.el-checkbox) {
  margin-right: 0;
}
.source-name,
.config-note {
  color: var(--lb-text-secondary);
  font-size: 12px;
}
.source-error {
  color: var(--el-color-warning-dark-2);
  font-size: 12px;
}
.mapping-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.mapping-row :deep(.el-input-number),
.mapping-row :deep(.el-select) {
  width: 100%;
}
.config-note {
  margin-top: 12px;
  line-height: 1.6;
}
@media (max-width: 600px) {
  .mapping-row {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
<style>
.utilization-dialog {
  max-width: calc(100vw - 24px);
}
</style>
