<template>
  <section class="traffic-history">
    <div class="history-controls">
      <el-radio-group v-model="preset" :aria-label="$t('trafficHistory.range')">
        <el-radio-button v-for="item in historyPresets" :key="item" :value="item">{{
          $t('trafficHistory.presets.' + item)
        }}</el-radio-button>
      </el-radio-group>
      <div v-if="preset === 'custom'" class="history-custom-range">
        <el-date-picker
          v-model="customRange"
          type="datetimerange"
          :start-placeholder="$t('trafficHistory.start')"
          :end-placeholder="$t('trafficHistory.end')"
        />
        <el-tooltip :content="$t('trafficHistory.inputZone', { zone: inputZone })">
          <button
            type="button"
            class="history-help"
            :aria-label="$t('trafficHistory.inputZone', { zone: inputZone })"
          >
            <el-icon><InfoFilled /></el-icon>
          </button>
        </el-tooltip>
      </div>
      <el-button :loading="loading" @click="refresh">{{ $t('common.refresh') }}</el-button>
    </div>
    <p v-if="!range" class="history-warning">{{ $t('trafficHistory.invalidRange') }}</p>
    <el-alert
      v-if="failed"
      type="warning"
      :closable="false"
      :title="$t('serviceTraffic.unavailable')"
    />
    <template v-if="snapshot">
      <p
        v-if="!snapshot.storage?.healthy || snapshot.storage?.query_truncated"
        class="history-warning"
      >
        {{ $t('trafficHistory.storageWarning') }}
      </p>
      <div class="history-summary">
        <div
          v-for="metric in ['requests', 'client_errors', 'server_errors', 'avg_ms', 'p95_ms']"
          :key="metric"
        >
          <span>{{ $t('trafficHistory.metrics.' + metric) }}</span
          ><strong :class="{ 'is-unknown-stat': !knownStat(metric) }"
            >{{
              knownStat(metric)
                ? number(snapshot.totals[metric])
                : $t('trafficHistory.notCollected')
            }}<small v-if="knownStat(metric) && metric.endsWith('_ms')"> ms</small></strong
          >
        </div>
      </div>
      <el-card shadow="never">
        <template #header>
          <div class="history-heading">
            <span>{{ $t('trafficHistory.trend') }}</span>
            <el-tooltip :content="coverageTooltip">
              <button type="button" class="history-help" :aria-label="coverageTooltip">
                <el-icon><InfoFilled /></el-icon>
              </button>
            </el-tooltip>
          </div>
        </template>
        <div class="history-layer-heading">
          <strong>{{ $t('trafficHistory.http') }}</strong
          ><span
            ><i class="key requests"></i>{{ $t('serviceTraffic.requests') }}
            <i class="key errors"></i>{{ $t('serviceTraffic.serverErrors') }}</span
          >
        </div>
        <div class="history-chart" role="group" :aria-label="$t('trafficHistory.http')">
          <el-tooltip
            v-for="point in rows"
            :key="point.at"
            :content="tooltip(point, false)"
            effect="light"
            :show-after="100"
          >
            <button
              class="history-bar"
              type="button"
              :class="{ 'is-unknown': !point.known }"
              :aria-label="tooltip(point, false)"
            >
              <template v-if="point.known"
                ><span class="requests" :style="{ height: height(point.requests, httpMax) }"></span
                ><span
                  class="errors"
                  :style="{ height: height(point.server_errors, httpMax) }"
                ></span
              ></template>
            </button>
          </el-tooltip>
        </div>
        <details class="history-source-breakdown">
          <summary>{{ $t('serviceTraffic.bySource') }}</summary>
          <div class="history-source-counts">
            <span v-for="source in httpSources" :key="source"
              >{{ $t('serviceTraffic.sources.' + source) }}
              <b>{{
                Number.isFinite(sourceCounts[source])
                  ? number(sourceCounts[source])
                  : $t('trafficHistory.notCollected')
              }}</b></span
            >
          </div>
        </details>
        <div class="history-layer-heading">
          <strong>{{ $t('trafficHistory.operations') }}</strong
          ><span v-for="source in sources" :key="source"
            ><i class="key" :class="source"></i>{{ $t('serviceTraffic.businessSources.' + source) }}
            <b>{{
              hasObservations ? number(operationCounts[source]) : $t('trafficHistory.notCollected')
            }}</b></span
          >
        </div>
        <div class="history-chart" role="group" :aria-label="$t('trafficHistory.operations')">
          <el-tooltip
            v-for="point in rows"
            :key="point.at"
            :content="tooltip(point, true)"
            effect="light"
            :show-after="100"
          >
            <button
              class="history-bar"
              type="button"
              :class="{ 'is-unknown': !point.known }"
              :aria-label="tooltip(point, true)"
            >
              <template v-if="point.known"
                ><span
                  v-for="(source, index) in sources"
                  :key="source"
                  :class="source"
                  :style="{
                    height: height(point.operations?.[source], operationMax),
                    bottom: height(
                      sources
                        .slice(0, index)
                        .reduce((sum, key) => sum + (point.operations?.[key] || 0), 0),
                      operationMax
                    ),
                  }"
                ></span
              ></template>
            </button>
          </el-tooltip>
        </div>
        <div v-if="rows.length" class="history-axis">
          <time :datetime="rows[0].at" :title="time(rows[0].at)">{{ axisTime(rows[0].at) }}</time>
          <time :datetime="rows.at(-1).at" :title="time(rows.at(-1).at)">{{
            axisTime(rows.at(-1).at)
          }}</time>
        </div>
      </el-card>
    </template>
    <el-empty v-else-if="!loading && range && !failed" :description="$t('common.noData')" />
  </section>
</template>
<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { InfoFilled } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
import { formatResourceTime } from '../utils/resourceTime'
import {
  createTrafficHistoryLoader,
  historyEndpoint,
  historyPresets,
  historyRange,
  historyRows,
  operationCount,
} from '../utils/serviceTrafficHistory'
const props = defineProps({ botId: { type: [Number, String], default: null }, admin: Boolean })
const { t, locale } = useI18n()
const inputZone = Intl.DateTimeFormat().resolvedOptions().timeZone
const preset = ref('24h')
const customRange = ref([])
const range = ref(null)
const snapshot = ref(null)
const loading = ref(false)
const failed = ref(false)
const sources = ['chat', 'agent', 'operator']
const httpSources = [
  'agent',
  'operator',
  'enrollment',
  'webhook',
  'resource_view',
  'management',
  'static',
  'health',
  'monitoring',
  'other',
]
const rows = computed(() => historyRows(snapshot.value))
const coverageTooltip = computed(() =>
  [
    t('trafficHistory.coverageHelp'),
    t('trafficHistory.coverage', snapshot.value?.coverage || {}),
    t('trafficHistory.resolution', { minutes: snapshot.value?.resolution_seconds / 60 }),
  ].join(' · ')
)
const axisFormatter = computed(
  () =>
    new Intl.DateTimeFormat(locale.value, {
      timeZone: inputZone,
      month: 'numeric',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    })
)
const axisTime = (value) => axisFormatter.value.format(new Date(value))
const hasObservations = computed(() => rows.value.some((point) => point.known))
const operationCounts = computed(() =>
  Object.fromEntries(
    sources.map((source) => [
      source,
      rows.value
        .filter((point) => point.known)
        .reduce((sum, point) => sum + (Number(point.operations?.[source]) || 0), 0),
    ])
  )
)
const sourceCounts = computed(() => {
  const observed = rows.value.filter((point) => point.known)
  return Object.fromEntries(
    httpSources.map((source) => [
      source,
      observed.length && observed.every((point) => Number.isFinite(point.sources?.[source]))
        ? observed.reduce((sum, point) => sum + point.sources[source], 0)
        : null,
    ])
  )
})
const knownStat = (metric) =>
  hasObservations.value && Number.isFinite(snapshot.value?.totals?.[metric])
const httpMax = computed(() =>
  Math.max(1, ...rows.value.filter((point) => point.known).map((point) => point.requests))
)
const operationMax = computed(() =>
  Math.max(1, ...rows.value.filter((point) => point.known).map(operationCount))
)
const loader = createTrafficHistoryLoader({
  request: async ({ botId, admin, start, end, resolution_seconds }, signal) => {
    const { data } = await api.get(historyEndpoint(botId, admin), {
      signal,
      params: { start, end, resolution_seconds, bot_id: admin ? botId || undefined : undefined },
    })
    return data
  },
  publish: (update) => {
    if ('snapshot' in update) snapshot.value = update.snapshot
    if ('loading' in update) loading.value = update.loading
    if ('failed' in update) failed.value = update.failed
  },
})
function refresh() {
  range.value = historyRange(preset.value, customRange.value || [])
  if (!range.value) {
    // A changed invalid range must also cancel and clear the previous authorized result.
    loader.setScope(null)
    return
  }
  return loader.setScope({ botId: props.botId, admin: props.admin, ...range.value })
}
watch([() => props.botId, () => props.admin, preset, customRange], refresh, {
  immediate: true,
  flush: 'sync',
})
onBeforeUnmount(() => loader.dispose())
const number = (value) =>
  Number.isFinite(value)
    ? new Intl.NumberFormat(locale.value, { maximumFractionDigits: 1 }).format(value)
    : '—'
const height = (value, max) => `${Math.min(100, (Math.max(0, Number(value) || 0) / max) * 100)}%`
const time = (value) => formatResourceTime(value, locale.value)
function tooltip(point, operations) {
  const values = !point.known
    ? [t('trafficHistory.unknown')]
    : operations
      ? sources.map(
          (source) =>
            `${t('serviceTraffic.businessSources.' + source)}: ${number(point.operations?.[source])}`
        )
      : [
          `${t('serviceTraffic.requests')}: ${number(point.requests)}`,
          `${t('serviceTraffic.serverErrors')}: ${number(point.server_errors)}`,
        ]
  return [time(point.at), ...values, t('trafficHistory.' + point.coverage)].join('\n')
}
</script>
<style scoped>
.history-controls,
.history-layer-heading,
.history-heading,
.history-axis {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.history-controls {
  margin-bottom: 16px;
}
.history-custom-range {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 380px;
  max-width: 100%;
}
.history-custom-range :deep(.el-date-editor) {
  flex: 1;
  min-width: 0;
}
.history-summary {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 16px;
  margin: 16px 0;
}
.history-summary > div {
  padding: 18px;
  border: 1px solid var(--lb-border-color);
  border-radius: 8px;
  background: var(--lb-bg-card);
}
.history-summary span {
  display: block;
  color: var(--lb-text-secondary);
  font-size: 13px;
}
.history-summary strong {
  display: block;
  font-size: 26px;
  margin-top: 8px;
}
.history-summary .is-unknown-stat {
  font-size: 14px;
  font-weight: 400;
  color: var(--lb-text-secondary);
}
.history-summary small {
  font-size: 12px;
  font-weight: 400;
}
.history-layer-heading {
  font-size: 13px;
  margin-bottom: 12px;
}
.history-layer-heading strong {
  margin-right: auto;
}
.history-layer-heading span {
  color: var(--lb-text-secondary);
}
.history-chart {
  height: 130px;
  display: flex;
  gap: 2px;
  margin-bottom: 24px;
  border-bottom: 1px solid var(--lb-border-color);
}
.history-bar {
  flex: 1;
  min-width: 0;
  padding: 0;
  position: relative;
  border: 0;
  background: transparent;
  cursor: help;
}
.history-bar > span {
  position: absolute;
  bottom: 0;
  left: 0;
  width: 100%;
}
.history-bar:hover,
.history-bar:focus-visible {
  background: var(--el-color-primary-light-9);
  outline: 1px solid var(--el-color-primary);
}
.history-bar.is-unknown {
  background: repeating-linear-gradient(
    135deg,
    transparent,
    transparent 3px,
    var(--lb-border-color) 3px,
    var(--lb-border-color) 4px
  );
}
.requests,
.agent {
  background: var(--el-color-primary);
}
.errors {
  background: var(--el-color-danger);
}
.chat {
  background: var(--el-color-success);
}
.operator {
  background: var(--el-color-warning);
}
.key {
  display: inline-block;
  width: 7px;
  height: 7px;
  margin-right: 5px;
  border-radius: 2px;
}
.history-axis {
  justify-content: space-between;
  font-size: 12px;
  color: var(--lb-text-secondary);
}
.history-help {
  display: inline-flex;
  padding: 2px;
  border: 0;
  background: transparent;
  color: var(--lb-text-secondary);
  cursor: help;
}
.history-source-breakdown {
  margin: -8px 0 22px;
  font-size: 12px;
  color: var(--lb-text-secondary);
}
.history-source-breakdown summary {
  cursor: pointer;
}
.history-source-counts {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 24px;
  padding-top: 12px;
}
.history-source-counts b {
  color: var(--lb-text-primary);
  margin-left: 4px;
}
.history-warning {
  font-size: 13px;
  color: var(--el-color-warning);
}
@media (max-width: 700px) {
  .history-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
