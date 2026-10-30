<template>
  <div class="service-traffic" :class="{ 'is-embedded': props.botId }">
    <div class="page-heading">
      <div>
        <h2>{{ $t('serviceTraffic.title') }}</h2>
        <p v-if="snapshot">
          {{ $t('serviceTraffic.since') }} {{ localTime(snapshot.started_at) }}
          <el-tooltip :content="$t('serviceTraffic.scopeHelp')"
            ><span tabindex="0"> ⓘ</span></el-tooltip
          >
        </p>
      </div>
      <el-button :loading="loading" @click="refresh"
        ><el-icon><Refresh /></el-icon>{{ $t('common.refresh') }}</el-button
      >
    </div>
    <el-select
      v-if="!props.botId"
      v-model="selectedBot"
      :disabled="loading"
      clearable
      :placeholder="$t('serviceTraffic.allBots')"
      class="bot-filter"
    >
      <el-option
        v-for="row in botOptions"
        :key="row.bot_id"
        :label="row.name ? `${row.name} (#${row.bot_id})` : `#${row.bot_id}`"
        :value="row.bot_id"
      />
    </el-select>
    <el-alert
      v-if="failed"
      :title="$t('serviceTraffic.unavailable')"
      type="warning"
      :closable="false"
    />
    <template v-if="snapshot">
      <h3 class="http-heading">{{ $t('serviceTraffic.httpTitle') }}</h3>
      <div class="traffic-stats">
        <div>
          <span>{{ $t('serviceTraffic.requests') }}</span
          ><strong>{{ number(snapshot.totals.requests) }}</strong>
        </div>
        <div>
          <span>{{ $t('serviceTraffic.lastMinute') }}</span
          ><strong>{{ number(snapshot.last_minute.requests) }}</strong>
        </div>
        <div>
          <span>{{ $t('serviceTraffic.p95') }}</span
          ><strong>{{ number(snapshot.totals.p95_ms) }}<small>ms</small></strong>
        </div>
        <div>
          <span>{{ $t('serviceTraffic.errorRate') }}</span
          ><strong :class="{ error: snapshot.totals.error_rate > 0 }"
            >{{ (snapshot.totals.error_rate * 100).toFixed(2) }}<small>%</small></strong
          >
        </div>
      </div>
      <el-card shadow="never" class="traffic-panel">
        <template #header
          ><div class="panel-heading">
            <span>{{ $t('serviceTraffic.trend') }}</span>
          </div></template
        >
        <div class="trend-layer-heading">
          <strong>{{ $t('serviceTraffic.httpLayer') }}</strong>
          <span class="chart-legend"
            ><i></i>{{ $t('serviceTraffic.requests') }}<i class="errors"></i
            >{{ $t('serviceTraffic.serverErrors') }}</span
          >
        </div>
        <div class="traffic-chart" role="group" :aria-label="$t('serviceTraffic.trend')">
          <span class="axis-high">{{ chartMax }}</span>
          <div class="traffic-bars">
            <template v-for="(point, index) in snapshot.history" :key="point.at">
              <el-tooltip
                :visible="activeMinute === point.at && activeChart === 'http'"
                :content="httpTooltip(point)"
                placement="top"
                effect="light"
                popper-class="traffic-minute-tooltip"
                virtual-triggering
                :virtual-ref="minuteReference"
                transition=""
                :show-arrow="false"
                :hide-after="0"
                :persistent="false"
              />
              <button
                type="button"
                class="traffic-minute"
                :class="{ 'is-active': activeMinute === point.at }"
                :data-bar-height="(point.requests / chartMax) * 100"
                :style="{ '--bar-height': `${(point.requests / chartMax) * 100}%` }"
                :tabindex="minuteTabIndex(point.at, index)"
                :aria-label="httpTooltip(point)"
                @pointerenter="enterMinute(point.at, 'http', $event)"
                @pointerdown="touchMinute(point.at, 'http', $event)"
                @pointerleave="leaveMinute($event)"
                @focus="selectMinute(point.at, 'http', $event.currentTarget)"
                @blur="clearMinute"
                @keydown="navigateMinute($event)"
              >
                <span
                  class="requests"
                  :style="{ height: `${(point.requests / chartMax) * 100}%` }"
                ></span>
                <span
                  class="errors"
                  :style="{ height: `${(point.server_errors / chartMax) * 100}%` }"
                ></span>
              </button>
            </template>
          </div>
        </div>
        <section class="operation-layer">
          <div class="trend-layer-heading">
            <strong
              >{{ $t('serviceTraffic.operationLayer') }}
              <el-tooltip :content="$t('serviceTraffic.operationTrendHelp')" placement="top">
                <el-icon class="metric-help" tabindex="0"><QuestionFilled /></el-icon>
              </el-tooltip>
            </strong>
            <span class="business-sources">
              <span
                v-for="source in operationSources"
                :key="source"
                class="operation-source"
                :class="`source-${source}`"
              >
                <i class="source-dot"></i>{{ $t('serviceTraffic.businessSources.' + source) }}
                <b>{{ number(operationTrend.totals[source]) }}</b>
              </span>
            </span>
          </div>
          <div
            v-if="operationTrend.hasActivity"
            class="traffic-chart operation-chart"
            role="group"
            :aria-label="$t('serviceTraffic.operationLayer')"
          >
            <span class="axis-high">{{ operationTrend.max }}</span>
            <div class="traffic-bars">
              <template v-for="(point, index) in operationTrend.history" :key="point.at">
                <el-tooltip
                  :visible="activeMinute === point.at && activeChart === 'operation'"
                  :content="operationTooltip(point)"
                  placement="top"
                  effect="light"
                  popper-class="traffic-minute-tooltip"
                  virtual-triggering
                  :virtual-ref="minuteReference"
                  transition=""
                  :show-arrow="false"
                  :hide-after="0"
                  :persistent="false"
                />
                <button
                  type="button"
                  class="traffic-minute"
                  :class="{
                    'is-unobserved': !point.observed,
                    'is-active': activeMinute === point.at,
                  }"
                  :data-bar-height="(point.total / operationTrend.max) * 100"
                  :style="{ '--bar-height': `${(point.total / operationTrend.max) * 100}%` }"
                  :tabindex="minuteTabIndex(point.at, index)"
                  :aria-label="operationTooltip(point)"
                  @pointerenter="enterMinute(point.at, 'operation', $event)"
                  @pointerdown="touchMinute(point.at, 'operation', $event)"
                  @pointerleave="leaveMinute($event)"
                  @focus="selectMinute(point.at, 'operation', $event.currentTarget)"
                  @blur="clearMinute"
                  @keydown="navigateMinute($event)"
                >
                  <span
                    v-for="segment in point.segments"
                    :key="segment.source"
                    class="operation-segment"
                    :class="`source-${segment.source}`"
                    :style="{ height: `${segment.height}%`, bottom: `${segment.bottom}%` }"
                  ></span>
                </button>
              </template>
            </div>
          </div>
          <p v-else class="trend-empty">
            {{
              $t(
                operationTrend.history.some((point) => point.observed)
                  ? 'serviceTraffic.noRecentOperations'
                  : 'serviceTraffic.operationHistoryUnavailable'
              )
            }}
          </p>
        </section>
        <div class="axis-times shared-axis">
          <span>{{ minute(snapshot.history[0]?.at) }}</span
          ><span>{{ minute(snapshot.history.at(-1)?.at) }}</span>
        </div>
        <details v-if="businessRows.length" class="operation-details">
          <summary>
            {{ $t('serviceTraffic.operationDetails') }}
            <el-tooltip :content="$t('serviceTraffic.businessHelp')" placement="top">
              <el-icon class="metric-help" tabindex="0"><QuestionFilled /></el-icon>
            </el-tooltip>
          </summary>
          <el-table :data="businessRows" :empty-text="$t('common.noData')" size="small">
            <el-table-column :label="$t('serviceTraffic.source')" min-width="130">
              <template #default="{ row }">
                <span class="operation-source" :class="`source-${row.source}`">
                  <el-icon><component :is="sourceIcons[row.source]" /></el-icon>
                  {{ $t('serviceTraffic.businessSources.' + row.source) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="command" :label="$t('agentHistory.operation')" min-width="120" />
            <el-table-column
              v-for="outcome in ['dispatched', 'rejected', 'uncertain']"
              :key="outcome"
              :label="$t('agentHistory.outcomes.' + outcome)"
              min-width="110"
              ><template #default="{ row }">{{ number(row[outcome]) }}</template></el-table-column
            >
          </el-table>
        </details>
      </el-card>
      <el-card shadow="never" class="traffic-panel">
        <el-tabs v-model="grouping"
          ><el-tab-pane :label="$t('serviceTraffic.bySource')" name="sources" /><el-tab-pane
            :label="$t('serviceTraffic.byRoute')"
            name="routes" /><el-tab-pane
            :label="$t('serviceTraffic.byCredential')"
            name="credentials"
        /></el-tabs>
        <el-table :data="rows" size="default">
          <el-table-column
            :label="
              grouping === 'sources'
                ? $t('serviceTraffic.source')
                : grouping === 'routes'
                  ? $t('serviceTraffic.route')
                  : $t('serviceTraffic.credential')
            "
            min-width="210"
            show-overflow-tooltip
            ><template #default="{ row }">
              <template v-if="grouping === 'credentials'">
                <div>
                  {{ row.user || $t('serviceTraffic.operator') }}
                  <span v-if="!props.botId">· #{{ row.bot_id }}</span>
                </div>
                <small :title="`${$t('serviceTraffic.since')} ${localTime(row.since)}`"
                  >{{ platformLabel(row.platform, $t)
                  }}<span v-if="row.id"> · {{ row.id }}</span></small
                >
              </template>
              <template v-else>{{
                grouping === 'sources' ? $t('serviceTraffic.sources.' + row.source) : row.route
              }}</template>
            </template></el-table-column
          >
          <el-table-column
            prop="requests"
            :label="$t('serviceTraffic.requests')"
            min-width="85"
            align="right"
          />
          <el-table-column label="2xx" min-width="65" align="right"
            ><template #default="{ row }">{{ row.statuses['2xx'] }}</template></el-table-column
          >
          <el-table-column label="4xx" min-width="65" align="right"
            ><template #default="{ row }">{{ row.statuses['4xx'] }}</template></el-table-column
          >
          <el-table-column label="5xx" min-width="65" align="right"
            ><template #default="{ row }"
              ><span :class="{ error: row.statuses['5xx'] }">{{
                row.statuses['5xx']
              }}</span></template
            ></el-table-column
          >
          <el-table-column :label="$t('serviceTraffic.average')" min-width="95" align="right"
            ><template #default="{ row }">{{ number(row.avg_ms) }} ms</template></el-table-column
          >
          <el-table-column :label="$t('serviceTraffic.received')" min-width="95" align="right"
            ><template #default="{ row }">{{ bytes(row.request_bytes) }}</template></el-table-column
          >
          <el-table-column :label="$t('serviceTraffic.sent')" min-width="95" align="right"
            ><template #default="{ row }">{{
              bytes(row.response_bytes)
            }}</template></el-table-column
          >
        </el-table>
        <div class="traffic-footer">
          <span v-if="!props.botId && !selectedBot"
            >{{ $t('serviceTraffic.unattributed') }}
            {{ number(snapshot.unattributed_requests) }}</span
          >
          <span
            >{{ $t('serviceTraffic.bodyTotals') }} {{ bytes(snapshot.totals.request_bytes) }} /
            {{ bytes(snapshot.totals.response_bytes) }}</span
          ><span>{{ $t('serviceTraffic.updated') }} {{ localTime(snapshot.captured_at) }}</span>
        </div>
      </el-card>
    </template>
    <el-empty v-else-if="!loading" :description="$t('common.noData')" />
  </div>
</template>

<script setup>
import { platformLabel } from '../utils/platformDisplay'
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { Refresh, ChatDotRound, Cpu, Setting, QuestionFilled } from '@element-plus/icons-vue'
const sourceIcons = { chat: ChatDotRound, agent: Cpu, operator: Setting }
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
import { createServiceTrafficLoader } from '../utils/serviceTrafficLoader'
import { buildOperationTrend, operationSources } from '../utils/operationTrend'
import { trafficBarReference } from '../utils/trafficBarReference'
const props = defineProps({
  botId: { type: [Number, String], default: null },
  includePrivate: { type: Boolean, default: false },
})
const { locale, t } = useI18n()
const selectedBot = ref(null)
const botOptions = ref([])
const snapshot = ref(null)
const loading = ref(false)
const failed = ref(false)
const grouping = ref('sources')
const activeMinute = ref(null)
const activeChart = ref(null)
const minuteReference = shallowRef(null)
function clearMinute() {
  activeMinute.value = null
  activeChart.value = null
  minuteReference.value = null
}
function selectMinute(at, chart, target = null) {
  activeMinute.value = at
  activeChart.value = chart
  minuteReference.value = target ? trafficBarReference(target) : null
}
function enterMinute(at, chart, event) {
  if (event.pointerType !== 'touch') selectMinute(at, chart, event.currentTarget)
}
function leaveMinute(event) {
  if (event.pointerType !== 'touch') clearMinute()
}
function touchMinute(at, chart, event) {
  if (event.pointerType !== 'touch') return
  event.preventDefault()
  if (activeMinute.value === at && activeChart.value === chart) clearMinute()
  else selectMinute(at, chart, event.currentTarget)
}
const minuteTabIndex = (at, index) =>
  activeMinute.value === at || (activeMinute.value === null && index === 0) ? 0 : -1
function navigateMinute(event) {
  if (event.key === 'Escape') {
    event.preventDefault()
    clearMinute()
    return
  }
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  const buttons = [
    ...event.currentTarget.closest('.traffic-bars').querySelectorAll('button.traffic-minute'),
  ]
  const index = buttons.indexOf(event.currentTarget)
  const next =
    event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? buttons.length - 1
        : Math.max(0, Math.min(buttons.length - 1, index + (event.key === 'ArrowLeft' ? -1 : 1)))
  event.preventDefault()
  buttons[next]?.focus()
}
function dismissMinute(event) {
  if (!event.target?.closest?.('.traffic-minute, .traffic-minute-tooltip')) clearMinute()
}

const rows = computed(() => snapshot.value?.[grouping.value] || [])
const businessRows = computed(() => {
  const grouped = new Map()
  for (const row of snapshot.value?.business_operations?.rows || []) {
    const key = `${row.source}:${row.command}`
    if (!grouped.has(key))
      grouped.set(key, {
        source: row.source,
        command: row.command,
        dispatched: 0,
        rejected: 0,
        uncertain: 0,
      })
    grouped.get(key)[row.outcome] += row.count
  }
  return [...grouped.values()]
})
const operationTrend = computed(() => buildOperationTrend(snapshot.value))
const httpTooltip = (point) =>
  [
    localTime(point.at),
    `${t('serviceTraffic.requests')}: ${number(point.requests)}`,
    `${t('serviceTraffic.serverErrors')}: ${number(point.server_errors)}`,
  ].join('\n')
const operationTooltip = (point) =>
  [
    localTime(point.at),
    ...(point.observed
      ? [
          ...operationSources.map(
            (source) =>
              `${t('serviceTraffic.businessSources.' + source)}: ${number(point.sources[source])}`
          ),
          `${t('serviceTraffic.minuteTotal')}: ${number(point.total)}`,
        ]
      : [t('serviceTraffic.operationHistoryUnavailable')]),
  ].join('\n')
const chartMax = computed(() =>
  Math.max(1, ...(snapshot.value?.history || []).map((point) => point.requests))
)
let poll = null
const scope = () => ({
  botId: props.botId || null,
  filterBot: props.botId ? null : selectedBot.value,
  includePrivate: false,
})
const loader = createServiceTrafficLoader({
  canRequest: () => !document.hidden,
  request: async ({ botId, filterBot, includePrivate }, signal) => {
    const endpoint = botId ? `/bots/${botId}/service-traffic` : '/admin/service-traffic'
    const { data } = await api.get(endpoint, {
      signal,
      params: { bot_id: filterBot || undefined, include_private: includePrivate },
    })
    return data
  },
  publish: (update) => {
    if ('snapshot' in update) {
      clearMinute()
      snapshot.value = update.snapshot
      if (update.snapshot?.bot_options) botOptions.value = update.snapshot.bot_options
    }
    if ('failed' in update) failed.value = update.failed
    if ('loading' in update) loading.value = update.loading
  },
})
const refresh = () => {
  clearMinute()
  return loader.refresh()
}
watch(grouping, clearMinute)
watch(
  [() => props.botId, selectedBot],
  ([botId], [previousBotId]) => {
    clearMinute()
    if (botId !== previousBotId) selectedBot.value = null
    loader.setScope(scope())
  },
  { flush: 'sync' }
)
const visible = () => {
  clearMinute()
  if (!document.hidden) refresh()
}
onMounted(() => {
  loader.setScope(scope())
  poll = setInterval(refresh, 15000)
  document.addEventListener('visibilitychange', visible)
  document.addEventListener('pointerdown', dismissMinute)
})
onBeforeUnmount(() => {
  clearMinute()
  loader.dispose()
  clearInterval(poll)
  document.removeEventListener('visibilitychange', visible)
  document.removeEventListener('pointerdown', dismissMinute)
})
const number = (value) =>
  new Intl.NumberFormat(locale.value, { maximumFractionDigits: 1 }).format(value || 0)
const localTime = (value) => (value ? new Date(value).toLocaleString(locale.value) : '—')
const minute = (value) =>
  value
    ? new Date(value).toLocaleTimeString(locale.value, { hour: '2-digit', minute: '2-digit' })
    : '—'
function bytes(value) {
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`
  return `${(value / 1024 ** 3).toFixed(1)} GB`
}
</script>

<style scoped>
.metric-help {
  margin-left: 4px;
  vertical-align: middle;
  color: var(--el-text-color-placeholder);
  cursor: help;
}
.http-heading {
  font-size: 15px;
  font-weight: 500;
}
.business-sources {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  color: var(--el-text-color-regular);
}
.operation-source {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.operation-source .el-icon {
  color: var(--source-color);
  font-size: 15px;
}
.source-chat {
  --source-color: var(--el-color-success);
}
.source-agent {
  --source-color: var(--el-color-primary);
}
.source-operator {
  --source-color: var(--el-color-warning);
}
.business-sources b {
  margin-left: 8px;
  color: var(--el-text-color-primary);
}

.page-heading,
.panel-heading,
.traffic-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.bot-filter {
  width: min(100%, 280px);
  margin-bottom: 12px;
}
.is-embedded h2 {
  font-size: 16px;
}
.page-heading {
  margin-bottom: 20px;
}
h2 {
  font-size: 22px;
  font-weight: 600;
  margin: 0;
}
.page-heading p,
.traffic-footer,
.chart-legend,
.axis-times,
.axis-high {
  font-size: 12px;
  color: var(--lb-text-secondary);
}
.page-heading p {
  margin-top: 8px;
}
.traffic-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin: 20px 0;
}
.traffic-stats > div {
  border: 1px solid var(--lb-border-color);
  background: var(--lb-bg-card);
  border-radius: 6px;
  padding: 20px;
}
.traffic-stats span {
  font-size: 13px;
  color: var(--lb-text-secondary);
}
.traffic-stats strong {
  display: block;
  margin-top: 10px;
  font-size: 28px;
  font-weight: 600;
  color: var(--lb-text-primary);
}
.traffic-stats small {
  font-size: 13px;
  font-weight: 400;
  margin-left: 4px;
}
.error,
.traffic-stats strong.error {
  color: var(--el-color-danger);
}
.traffic-panel {
  margin-top: 20px;
}
.chart-legend {
  display: flex;
  align-items: center;
  gap: 6px;
}
.chart-legend i {
  display: inline-block;
  width: 10px;
  height: 3px;
  background: var(--el-color-primary);
  margin-left: 12px;
}
.chart-legend .errors {
  background: var(--el-color-danger);
}
.traffic-chart {
  position: relative;
  padding: 14px 0 0 25px;
}
.axis-high {
  position: absolute;
  left: 0;
  top: 0;
}
.traffic-bars {
  height: 160px;
  display: flex;
  align-items: flex-end;
  gap: 3px;
  border-bottom: 1px solid var(--lb-border-color);
  background: repeating-linear-gradient(
    to top,
    transparent 0,
    transparent 39px,
    var(--lb-border-color) 40px
  );
}
.traffic-minute {
  position: relative;
  height: 100%;
  flex: 1;
  min-width: 0;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  cursor: pointer;
  transition: background-color 150ms ease;
}
/* Full-height button is a hit area, never a visible value bar. */
.traffic-minute.is-active::after,
.traffic-minute:focus-visible::after {
  content: '';
  position: absolute;
  bottom: 0;
  left: 0;
  width: 100%;
  height: max(2px, var(--bar-height, 0%));
  box-sizing: border-box;
  border: 1px solid var(--el-color-primary);
  pointer-events: none;
}
.traffic-minute:focus-visible {
  outline: none;
}
.traffic-minute:focus-visible::after {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 1px;
}
.traffic-minute.is-active span {
  opacity: 0.78;
}
:global(.traffic-minute-tooltip) {
  white-space: pre-line;
  max-width: min(260px, calc(100vw - 24px));
  pointer-events: none;
}
.traffic-minute span {
  position: absolute;
  bottom: 0;
  width: 100%;
  background: var(--el-color-primary);
  pointer-events: none;
  transition: opacity 150ms ease;
}
.traffic-minute .operation-segment {
  background: var(--source-color);
}
.traffic-minute.is-unobserved::before {
  content: '';
  position: absolute;
  bottom: 0;
  left: 0;
  width: 100%;
  border-bottom: 2px dotted var(--el-border-color);
}
.traffic-minute .errors {
  background: var(--el-color-danger);
}
.trend-layer-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 13px;
}
.operation-layer {
  border-top: 1px solid var(--lb-border-color);
  margin-top: 20px;
  padding-top: 16px;
}
.operation-chart .traffic-bars {
  height: 100px;
}
.source-dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: var(--source-color);
}
.trend-note,
.trend-empty {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}
.trend-empty {
  margin: 12px 0 4px;
}
.shared-axis {
  padding-left: 25px;
}
.operation-details {
  margin-top: 16px;
  font-size: 13px;
}
.operation-details summary {
  cursor: pointer;
  color: var(--el-text-color-regular);
}
.axis-times {
  display: flex;
  justify-content: space-between;
  margin-top: 8px;
}
.traffic-footer {
  margin-top: 16px;
  flex-wrap: wrap;
}
@media (max-width: 760px) {
  .traffic-stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }
  .traffic-stats > div {
    padding: 14px;
  }
  .traffic-stats strong {
    font-size: 24px;
  }
  .panel-heading {
    align-items: flex-start;
    flex-wrap: wrap;
  }
}
@media (prefers-reduced-motion: reduce) {
  .traffic-minute,
  .traffic-minute span {
    transition: none;
  }
}
</style>
