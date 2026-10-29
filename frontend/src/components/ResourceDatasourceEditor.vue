<template>
  <section class="utilization-control">
    <div class="utilization-toolbar">
      <div class="source-summary">
        <strong>{{ $t('utilization.title') }}</strong>
        <span class="source-name">{{ modelValue?.name || $t('utilization.notConfigured') }}</span>
      </div>
      <div class="source-actions">
        <el-button size="small" @click="openSettings">{{ $t('utilization.configure') }}</el-button>
        <el-button
          v-if="modelValue"
          text
          size="small"
          type="danger"
          :disabled="testing"
          @click="removeSource"
          >{{ $t('utilization.remove') }}</el-button
        >
      </div>
    </div>
    <el-dialog
      v-model="settings"
      :title="$t('utilization.configure')"
      width="640px"
      class="utilization-dialog"
    >
      <p class="config-note">{{ $t('utilization.templateHint') }}</p>
      <a
        class="setup-guide"
        href="https://github.com/dynamicheart/lockbot/blob/release/2.x/docs/usage/browser-utilization.md"
        target="_blank"
        rel="noopener noreferrer"
        >{{ $t('utilization.setupGuide') }}</a
      >
      <el-form label-position="top" @submit.prevent.stop="testAndEnable">
        <el-form-item :label="$t('utilization.template')">
          <el-select v-model="preset" @change="applyPreset">
            <el-option value="lockbot-v1" :label="$t('utilization.profileLockbot')" />
            <el-option value="dcgm" label="NVIDIA DCGM" />
            <el-option value="custom" :label="$t('utilization.profileCustom')" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('utilization.name')"
          ><el-input v-model="draft.name" maxlength="64"
        /></el-form-item>
        <el-form-item :label="$t('utilization.endpoint')"
          ><el-input v-model="draft.endpoint" placeholder="https://prometheus.example.com"
        /></el-form-item>
        <el-form-item :label="$t('utilization.accessMode')">
          <el-select v-model="draft.accessMode">
            <el-option value="short_lived" :label="$t('utilization.accessAutomatic')" />
            <el-option value="independent" :label="$t('utilization.accessIndependent')" />
          </el-select>
        </el-form-item>
        <el-form-item
          v-if="draft.accessMode !== 'short_lived'"
          :label="$t('utilization.sourceToken')"
          ><el-input v-model="bearer" type="password" show-password autocomplete="off"
        /></el-form-item>
        <el-form-item :label="$t('utilization.nodeMatch')">
          <div class="mapping-table">
            <div v-for="node in nodes" :key="node" class="mapping-entry">
              <strong>{{ node }}</strong>
              <el-input
                v-model="nodeMapping[node]"
                :aria-label="node + ' ' + $t('utilization.targetNode')"
                :placeholder="$t('utilization.targetNode')"
              />
            </div>
          </div>
        </el-form-item>
        <el-form-item v-if="deviceResources" :label="$t('utilization.deviceMatch')">
          <div class="mapping-table">
            <div v-for="id in resources" :key="id" class="mapping-entry">
              <span>{{ id }}</span
              ><el-input
                v-model="deviceMapping[id]"
                :aria-label="id + ' ' + $t('utilization.deviceLabel')"
              />
            </div>
          </div>
        </el-form-item>
        <details class="mapping-import">
          <summary>{{ $t('utilization.importMapping') }}</summary>
          <el-input
            v-model="nodeMappingText"
            type="textarea"
            :rows="4"
            :placeholder="$t('utilization.nodeMapping')"
          />
          <el-button size="small" @click="importMapping">{{
            $t('utilization.applyMapping')
          }}</el-button>
        </details>
        <el-collapse v-if="preset === 'custom'">
          <el-collapse-item :title="$t('utilization.metrics')" name="metrics">
            <div class="mapping-row">
              <el-form-item :label="$t('utilization.nodeLabel')"
                ><el-input v-model="draft.nodeLabel"
              /></el-form-item>
              <el-form-item :label="$t('utilization.deviceLabel')"
                ><el-input v-model="draft.deviceLabel"
              /></el-form-item>
            </div>
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
        <p v-if="validationIssues.length" class="source-error">
          {{ $t('utilization.checkResources') }}: {{ validationIssues.join(', ') }}
        </p>
        <p v-if="configError" class="source-error">{{ $t('utilization.errors.' + configError) }}</p>
      </el-form>
      <template #footer>
        <el-button @click="settings = false">{{ $t('common.cancel') }}</el-button>
        <el-button
          type="primary"
          :loading="testing"
          :disabled="!nodes.length"
          @click="testAndEnable"
          >{{ $t('utilization.testSave') }}</el-button
        >
      </template>
    </el-dialog>
  </section>
</template>
<script setup>
import { reactive, ref, watch, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { sourceHealth } from '../utils/utilization/display'
import {
  DEFAULT_CONFIG,
  validateConfig,
  prometheusDatasource,
  resolveCards,
  ProviderError,
  metricStatus,
} from '../utils/utilization/prometheus'
const props = defineProps({
  modelValue: { type: Object, default: null },
  nodes: { type: Array, default: () => [] },
  resources: { type: Array, default: () => [] },
  deviceResources: Boolean,
  save: { type: Function, default: null },
  authorize: { type: Function, default: null },
})
const emit = defineEmits(['update:modelValue'])
const { t } = useI18n()
const preset = ref('lockbot-v1')
const nodeMapping = reactive({})
const deviceMapping = reactive({})
const validationIssues = ref([])
const settings = ref(false)
const draft = reactive({ ...DEFAULT_CONFIG })
const nodeMappingText = ref('{}')
const bearer = ref('')
const configError = ref(null)
const testing = ref(false)
let controller
let scope = 0
let nextTestAt = 0
function cancelTest() {
  scope++
  controller?.abort()
  testing.value = false
  bearer.value = ''
}
async function removeSource() {
  testing.value = true
  try {
    await props.save?.(null)
    emit('update:modelValue', null)
  } catch {
    // The parent displays a save error and retains the current source.
  } finally {
    testing.value = false
  }
}
function importMapping() {
  try {
    const value = JSON.parse(nodeMappingText.value)
    if (
      !value ||
      Array.isArray(value) ||
      typeof value !== 'object' ||
      Object.values(value).some((v) => typeof v !== 'string')
    )
      throw new Error()
    for (const node of props.nodes) if (Object.hasOwn(value, node)) nodeMapping[node] = value[node]
    configError.value = null
  } catch {
    configError.value = 'configuration'
  }
}
function applyPreset() {
  draft.profile = preset.value === 'lockbot-v1' ? 'lockbot-v1' : 'custom'
  if (preset.value === 'lockbot-v1') {
    Object.assign(
      draft,
      validateConfig({
        ...draft,
        endpoint: draft.endpoint || 'https://metrics.example.invalid',
        nodeMapping: { node: 'node' },
      }),
      {
        endpoint: draft.endpoint,
        intervalSeconds: 60,
        staleSeconds: 660,
        accessMode: 'short_lived',
      }
    )
  } else if (preset.value === 'dcgm') {
    const endpoint = draft.endpoint
    Object.assign(draft, DEFAULT_CONFIG, { endpoint, name: 'NVIDIA DCGM', intervalSeconds: 60 })
  }
  for (const id of props.resources)
    deviceMapping[id] = preset.value === 'lockbot-v1' ? id.match(/:dev([0-9]+)$/)?.[1] || '' : ''
}
function openSettings() {
  cancelTest()
  Object.assign(draft, props.modelValue || DEFAULT_CONFIG)
  preset.value =
    props.modelValue?.profile === 'lockbot-v1' || !props.modelValue
      ? 'lockbot-v1'
      : props.modelValue?.utilizationMetric === DEFAULT_CONFIG.utilizationMetric
        ? 'dcgm'
        : 'custom'
  for (const key of Object.keys(nodeMapping)) delete nodeMapping[key]
  for (const key of Object.keys(deviceMapping)) delete deviceMapping[key]
  if (!props.modelValue) applyPreset()
  for (const node of props.nodes) nodeMapping[node] = props.modelValue?.nodeMapping?.[node] || node
  for (const id of props.resources)
    deviceMapping[id] =
      props.modelValue?.deviceMapping?.[id] ||
      (preset.value === 'lockbot-v1' ? id.match(/:dev([0-9]+)$/)?.[1] || '' : '')
  nodeMappingText.value = JSON.stringify(nodeMapping, null, 2)
  validationIssues.value = []
  configError.value = null
  settings.value = true
}
async function testAndEnable() {
  if (testing.value || !props.nodes.length || document.hidden) return
  if (performance.now() < nextTestAt) {
    configError.value = 'limited'
    return
  }
  let candidate
  try {
    candidate = validateConfig({
      ...draft,
      nodeMapping: Object.fromEntries(props.nodes.map((node) => [node, nodeMapping[node]?.trim()])),
      deviceMapping: props.deviceResources
        ? Object.fromEntries(props.resources.map((id) => [id, deviceMapping[id]?.trim()]))
        : {},
    })
  } catch (error) {
    configError.value = error.code || 'configuration'
    return
  }
  const run = ++scope
  controller?.abort()
  const current = new AbortController()
  controller = current
  testing.value = true
  configError.value = null
  nextTestAt = performance.now() + Math.max(30, candidate.intervalSeconds) * 1000
  const timeout = setTimeout(() => current.abort(), 15000)
  try {
    let sourceBearer = bearer.value
    if (candidate.accessMode === 'short_lived') {
      try {
        sourceBearer = (await props.authorize?.(candidate))?.token
      } catch {
        throw new ProviderError('grant')
      }
    }
    if (candidate.accessMode === 'short_lived' && !sourceBearer) throw new ProviderError('grant')
    const snapshot = await prometheusDatasource.testDatasource(candidate, props.nodes, {
      signal: current.signal,
      bearer: sourceBearer,
    })
    if (run !== scope || current.signal.aborted) return
    const health = sourceHealth(snapshot, candidate, props.nodes, Date.now(), metricStatus)
    const missingDevices = props.deviceResources
      ? props.resources.filter((id) => {
          const cards = resolveCards(snapshot, candidate, id.replace(/:dev[0-9]+$/, ''), id)
          return (
            cards.length !== 1 ||
            Object.values(cards[0].metrics).some(
              (metric) => metricStatus(metric, Date.now(), candidate.staleSeconds) !== 'ok'
            )
          )
        })
      : []
    validationIssues.value = [...health.issues.map((item) => item.node), ...missingDevices]
    if (validationIssues.value.length) throw new ProviderError('samples')
    await props.save?.(candidate)
    emit('update:modelValue', candidate)
    ElMessage.success(
      t('utilization.validationSuccess', {
        nodes: props.nodes.length,
        cards: snapshot.cards.length,
      })
    )
    settings.value = false
  } catch (error) {
    if (run === scope)
      configError.value = current.signal.aborted ? 'timeout' : error.code || 'network'
  } finally {
    clearTimeout(timeout)
    current.abort()
    if (run === scope) {
      testing.value = false
      bearer.value = ''
    }
  }
}
watch(settings, (value) => {
  if (!value) cancelTest()
})
watch(
  () => JSON.stringify([props.nodes, props.resources]),
  () => {
    cancelTest()
    settings.value = false
  }
)
const visibility = () => {
  if (document.hidden) cancelTest()
}
document.addEventListener('visibilitychange', visibility)
onBeforeUnmount(() => {
  cancelTest()
  document.removeEventListener('visibilitychange', visibility)
})
</script>
<style scoped>
.utilization-control {
  width: 100%;
}
.utilization-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-start;
  gap: 12px;
}
.source-summary,
.source-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.source-summary strong {
  font-size: 14px;
  font-weight: 500;
}
.setup-guide {
  display: inline-block;
  color: var(--el-color-primary);
  font-size: 12px;
  text-decoration: none;
  margin-bottom: 16px;
}
.setup-guide:hover {
  text-decoration: underline;
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
.mapping-table {
  width: 100%;
  display: grid;
  gap: 8px;
}
.mapping-entry {
  display: grid;
  grid-template-columns: minmax(90px, 1fr) minmax(150px, 2fr);
  gap: 12px;
  align-items: center;
}
.mapping-entry > :first-child {
  overflow-wrap: anywhere;
  font-size: 13px;
}
.mapping-import {
  margin: 16px 0;
  font-size: 12px;
  color: var(--lb-text-secondary);
}
.mapping-import summary {
  cursor: pointer;
  margin-bottom: 8px;
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
.utilization-dialog .el-dialog__body {
  max-height: min(65vh, 680px);
  overflow-y: auto;
}
.utilization-dialog .el-select {
  width: 100%;
}
</style>
