<template>
  <div v-if="enabled" class="resource-metrics">
    <span v-if="error" class="is-muted">{{ $t('utilization.unavailable') }}</span>
    <template v-else>
      <div v-if="host && !resourceId" class="host-metrics">
        <span :title="$t('utilization.hostMemory') + ': ' + memoryText(host)">
          CPU {{ metricText(host.cpu, true) }} · {{ $t('utilization.hostMemory') }}
          {{ memoryText(host) }}
        </span>
        <span
          >{{ $t('utilization.availableMemory') }} {{ metricText(host.available) }} ·
          {{ $t('utilization.cacheMemory') }} {{ metricText(host.cache) }}</span
        >
        <div v-if="host.filesystems?.length" class="filesystem-metrics">
          <span v-for="disk in host.filesystems" :key="disk.name" :title="memoryText(disk)">
            {{ $t('utilization.disk') }} {{ disk.name }} {{ memoryPercent(disk) }}
          </span>
        </div>
      </div>
      <span v-if="!cards.length" class="is-muted">—</span>
      <div class="card-metrics-list">
        <div v-for="card in cards" :key="card.device" class="card-metric">
          <div class="card-values">
            <span class="device-name" :title="deviceName(card.device)">{{
              deviceName(card.device)
            }}</span>
            <span
              v-if="Object.values(card.metrics).every((metric) => state(metric) !== 'ok')"
              class="is-muted"
              >{{ metricText(card.metrics.compute, true) }}</span
            >
            <template v-else>
              <strong :class="loadClass(card)">{{ metricText(card.metrics.compute, true) }}</strong>
              <span class="metrics-memory" :class="capacityClass(card.metrics)">{{
                memoryText(card.metrics)
              }}</span>
            </template>
          </div>
          <el-popover
            v-if="card.processes?.length || card.processesTruncated"
            trigger="click"
            popper-class="resource-process-popover"
            placement="left"
            :width="560"
          >
            <template #reference>
              <button class="process-link" type="button">
                {{ $t('utilization.processCount', { count: card.processes.length })
                }}{{ card.processesTruncated ? '+' : '' }}
              </button>
            </template>
            <div class="process-detail">
              <strong>{{ nodeId }} · {{ deviceName(card.device) }}</strong>
              <p>{{ $t('utilization.processNote') }}</p>
              <table>
                <thead>
                  <tr>
                    <th>{{ $t('utilization.container') }}</th>
                    <th>{{ $t('utilization.hostUser') }}</th>
                    <th>PID</th>
                    <th>{{ $t('utilization.command') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(process, index) in card.processes" :key="index">
                    <td :title="process.containerId">
                      {{
                        process.containerName ||
                        process.containerId ||
                        $t('utilization.hostProcess')
                      }}
                    </td>
                    <td>{{ process.user || '—' }}</td>
                    <td>{{ process.pid || '—' }}</td>
                    <td>
                      <code>{{ process.command || '—' }}</code>
                    </td>
                  </tr>
                </tbody>
              </table>
              <p v-if="card.processesTruncated">{{ $t('utilization.processTruncated') }}</p>
              <p>{{ $t('utilization.sampled') }}: {{ sampleTime(card) }}</p>
            </div>
          </el-popover>
        </div>
      </div>
    </template>
  </div>
</template>
<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { loadLevel } from '../utils/utilization/display'
import { metricStatus, resolveCards, resolveHost } from '../utils/utilization/prometheus'
const props = defineProps({
  enabled: Boolean,
  config: { type: Object, default: null },
  snapshot: { type: Object, default: null },
  error: { type: String, default: null },
  nodeId: { type: String, required: true },
  resourceId: { type: String, default: null },
  now: { type: Number, default: Date.now },
})
const { t, locale } = useI18n()
const cards = computed(() =>
  resolveCards(props.snapshot, props.config, props.nodeId, props.resourceId)
)
const host = computed(() => resolveHost(props.snapshot, props.config, props.nodeId))
const state = (metric) => metricStatus(metric, props.now, props.config?.staleSeconds || 90)
const deviceName = (device) => (/^\d+$/.test(device) ? `dev${device}` : device)
function metricText(metric, percent = false) {
  const status = state(metric)
  if (status !== 'ok')
    return status === 'stale'
      ? t('utilization.stale')
      : status === 'failed'
        ? t('utilization.unavailable')
        : status === 'invalid'
          ? t('utilization.invalid')
          : '—'
  return percent ? `${Math.round(metric.value)}%` : `${(metric.value / 1024 ** 3).toFixed(1)} GiB`
}
function capacityClass(metrics) {
  if (state(metrics.used) !== 'ok' || state(metrics.total) !== 'ok' || metrics.total.value <= 0)
    return 'is-muted'
  const ratio = metrics.used.value / metrics.total.value
  return ratio >= 0.9 ? 'load-high' : ratio >= 0.75 ? 'load-medium' : 'is-muted'
}
function memoryText(metrics) {
  return `${metricText(metrics.used)} / ${metricText(metrics.total)}`
}
function memoryPercent(metrics) {
  return state(metrics.used) === 'ok' && state(metrics.total) === 'ok' && metrics.total.value > 0
    ? `${Math.round((metrics.used.value / metrics.total.value) * 100)}%`
    : state(metrics.total) !== 'ok'
      ? metricText(metrics.total)
      : metricText(metrics.used)
}
function sampleTime(card) {
  const times = Object.values(card.metrics)
    .map((metric) => metric.observedAt)
    .filter(Number.isFinite)
  return times.length ? new Date(Math.min(...times)).toLocaleString(locale.value) : '—'
}
function loadClass(card) {
  const level = loadLevel([card], props.now, props.config?.staleSeconds || 90, metricStatus)
  return level === 'unknown' ? 'is-muted' : `load-${level}`
}
</script>
<style scoped>
.resource-metrics {
  display: grid;
  gap: 5px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.card-metric {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.card-metrics-list {
  display: grid;
  gap: 3px;
  max-width: 520px;
}
.card-values {
  display: grid;
  flex: 1;
  grid-template-columns: 36px 34px minmax(0, 1fr);
  align-items: baseline;
  gap: 5px;
}
.card-values > .is-muted {
  grid-column: 2 / -1;
  white-space: nowrap;
}
.device-name {
  color: var(--lb-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.metrics-memory,
.is-muted,
.host-metrics {
  color: var(--lb-text-secondary);
}
.host-metrics {
  display: grid;
  gap: 3px;
  margin-bottom: 5px;
}
.filesystem-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
}
.load-low {
  color: var(--el-color-success-dark-2);
}
.load-medium {
  color: var(--el-color-warning-dark-2);
}
.load-high {
  color: var(--el-color-danger);
}
.process-link {
  border: 0;
  background: transparent;
  color: var(--el-color-primary);
  font: inherit;
  padding: 0;
  white-space: nowrap;
  cursor: pointer;
}
.process-detail {
  max-height: 340px;
  overflow: auto;
  font-size: 12px;
}
.process-detail table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
}
.process-detail td,
.process-detail th {
  padding: 6px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  overflow-wrap: anywhere;
}
.process-detail p {
  color: var(--lb-text-secondary);
  line-height: 1.5;
}
</style>

<style>
.resource-process-popover {
  max-width: calc(100vw - 24px);
}
</style>
