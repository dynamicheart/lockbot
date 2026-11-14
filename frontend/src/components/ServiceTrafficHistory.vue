<template>
  <section class="traffic-history">
    <div class="history-controls">
      <el-radio-group v-model="preset" :aria-label="$t('trafficHistory.range')">
        <el-radio-button v-for="item in historyPresets" :key="item" :value="item">{{
          $t('trafficHistory.presets.' + item)
        }}</el-radio-button>
      </el-radio-group>
      <el-date-picker
        v-if="preset === 'custom'"
        v-model="customRange"
        type="datetimerange"
        :start-placeholder="$t('trafficHistory.start')"
        :end-placeholder="$t('trafficHistory.end')"
      />
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
          ><strong
            >{{ number(hasObservations ? snapshot.totals?.[metric] : null)
            }}<small v-if="metric.endsWith('_ms')"> ms</small></strong
          >
        </div>
      </div>
      <el-card shadow="never">
        <template #header
          ><div class="history-heading">
            <span>{{ $t('trafficHistory.trend') }}</span
            ><el-tooltip :content="$t('trafficHistory.coverageHelp')"
              ><span tabindex="0" class="history-help">ⓘ</span></el-tooltip
            >
          </div></template
        >
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
              :class="{ 'is-unknown': !point.known, 'is-partial': point.coverage === 'partial' }"
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
        <div class="history-layer-heading">
          <strong>{{ $t('trafficHistory.operations') }}</strong
          ><span v-for="source in sources" :key="source"
            ><i class="key" :class="source"></i
            >{{ $t('serviceTraffic.businessSources.' + source) }}</span
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
              :class="{ 'is-unknown': !point.known, 'is-partial': point.coverage === 'partial' }"
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
        <div class="history-axis">
          <span>{{ time(rows[0]?.at) }}</span
          ><span>{{ time(rows.at(-1)?.at) }}</span>
        </div>
        <p class="history-note">
          {{ $t('trafficHistory.coverage', snapshot.coverage || {}) }} ·
          {{ $t('trafficHistory.resolution', { minutes: snapshot.resolution_seconds / 60 }) }}
        </p>
      </el-card>
    </template>
    <el-empty v-else-if="!loading && range && !failed" :description="$t('common.noData')" />
  </section>
</template>
<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
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
const preset = ref('24h')
const customRange = ref([])
const range = ref(null)
const snapshot = ref(null)
const loading = ref(false)
const failed = ref(false)
const sources = ['chat', 'agent', 'operator']
const rows = computed(() => historyRows(snapshot.value))
const hasObservations = computed(() => rows.value.some((point) => point.known))
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
const time = (value) =>
  value ? new Date(value).toISOString().replace('T', ' ').replace('.000Z', ' UTC') : '—'
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
.history-bar.is-partial {
  border-top: 2px dotted var(--el-color-warning);
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
.history-note,
.history-help {
  font-size: 12px;
  color: var(--lb-text-secondary);
}
.history-warning {
  font-size: 13px;
  color: var(--el-color-warning);
}
@media (max-width: 700px) {
  .history-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .history-controls :deep(.el-date-editor) {
    width: 100%;
  }
}
</style>
