<template>
  <el-tooltip v-if="enabled" placement="top" effect="light">
    <template #content>
      <div class="metrics-detail">
        <span v-if="error">{{ $t('utilization.unavailable') }}</span>
        <span v-else-if="!cards.length">{{ $t('utilization.unmapped') }}</span>
        <div v-for="card in cards" :key="card.device">
          <strong>{{ card.device }}</strong>
          <span>{{ $t('utilization.compute') }}: {{ metricText(card.metrics.compute, true) }}</span>
          <span>{{ $t('utilization.memory') }}: {{ memoryText(card) }}</span>
          <span>{{ $t('utilization.sampled') }}: {{ sampleTime(card) }}</span>
        </div>
      </div>
    </template>
    <span class="metrics-value" :class="{ 'is-muted': error || !healthy }" tabindex="0">
      <template v-if="error">{{ $t('utilization.unavailable') }}</template>
      <template v-else-if="!cards.length">—</template>
      <template v-else-if="allUnavailable">{{ unavailableText }}</template>
      <template v-else-if="resourceId">
        {{ metricText(cards[0].metrics.compute, true) }}
        <span class="metrics-memory">{{ memoryText(cards[0]) }}</span>
      </template>
      <template v-else>
        {{ $t('utilization.peak') }} {{ peak }}
        <span class="metrics-memory">{{ $t('utilization.cards', { count: cards.length }) }}</span>
      </template>
    </span>
  </el-tooltip>
</template>
<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { metricStatus, resolveCards } from '../utils/utilization/prometheus'
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
const state = (metric) => metricStatus(metric, props.now, props.config?.staleSeconds || 90)
const healthy = computed(() =>
  cards.value.every((card) => Object.values(card.metrics).every((metric) => state(metric) === 'ok'))
)
function metricText(metric, percent = false) {
  const status = state(metric)
  if (status !== 'ok')
    return status === 'stale'
      ? t('utilization.stale')
      : status === 'invalid'
        ? t('utilization.invalid')
        : '—'
  return percent ? `${Math.round(metric.value)}%` : `${(metric.value / 1024 ** 3).toFixed(1)} GiB`
}
function memoryText(card) {
  return `${metricText(card.metrics.used)} / ${metricText(card.metrics.total)}`
}
function sampleTime(card) {
  const times = Object.values(card.metrics)
    .map((metric) => metric.observedAt)
    .filter(Number.isFinite)
  return times.length ? new Date(Math.min(...times)).toLocaleString(locale.value) : '—'
}
const allUnavailable = computed(
  () =>
    cards.value.length &&
    cards.value.every((card) =>
      Object.values(card.metrics).every((metric) => state(metric) !== 'ok')
    )
)
const unavailableText = computed(() => {
  const states = cards.value.flatMap((card) => Object.values(card.metrics).map(state))
  return states.includes('invalid')
    ? t('utilization.invalid')
    : states.includes('stale')
      ? t('utilization.stale')
      : '—'
})
const peak = computed(() => {
  // An incomplete node cannot advertise the maximum of only the successful cards.
  if (!cards.value.length || cards.value.some((card) => state(card.metrics.compute) !== 'ok'))
    return '—'
  return `${Math.round(Math.max(...cards.value.map((card) => card.metrics.compute.value)))}%`
})
</script>
<style scoped>
.metrics-value {
  display: inline-flex;
  flex-direction: column;
  gap: 3px;
  font-variant-numeric: tabular-nums;
  color: var(--lb-text-primary);
  font-size: 12px;
}
.metrics-memory,
.is-muted {
  color: var(--lb-text-secondary);
}
.metrics-detail {
  max-width: 360px;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.metrics-detail > div {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 0;
}
</style>
