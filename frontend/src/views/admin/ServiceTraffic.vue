<template>
  <div class="service-traffic">
    <div class="page-heading">
      <div>
        <h2>{{ $t('serviceTraffic.title') }}</h2>
        <p v-if="snapshot">{{ $t('serviceTraffic.since') }} {{ localTime(snapshot.started_at) }}</p>
      </div>
      <el-button :loading="loading" @click="refresh"
        ><el-icon><Refresh /></el-icon>{{ $t('common.refresh') }}</el-button
      >
    </div>
    <el-alert
      v-if="failed"
      :title="$t('serviceTraffic.unavailable')"
      type="warning"
      :closable="false"
    />
    <template v-if="snapshot">
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
            <span>{{ $t('serviceTraffic.trend') }}</span
            ><span class="chart-legend"
              ><i></i>{{ $t('serviceTraffic.requests') }}<i class="errors"></i
              >{{ $t('serviceTraffic.serverErrors') }}</span
            >
          </div></template
        >
        <div class="traffic-chart" role="img" :aria-label="$t('serviceTraffic.trend')">
          <span class="axis-high">{{ chartMax }}</span>
          <div class="traffic-bars">
            <div
              v-for="point in snapshot.history"
              :key="point.at"
              class="traffic-minute"
              :title="`${localTime(point.at)} · ${point.requests} / ${point.server_errors}`"
            >
              <span
                class="requests"
                :style="{ height: `${(point.requests / chartMax) * 100}%` }"
              ></span>
              <span
                class="errors"
                :style="{ height: `${(point.server_errors / chartMax) * 100}%` }"
              ></span>
            </div>
          </div>
          <div class="axis-times">
            <span>{{ minute(snapshot.history[0]?.at) }}</span
            ><span>{{ minute(snapshot.history.at(-1)?.at) }}</span>
          </div>
        </div>
      </el-card>
      <el-card shadow="never" class="traffic-panel">
        <el-tabs v-model="grouping"
          ><el-tab-pane :label="$t('serviceTraffic.bySource')" name="sources" /><el-tab-pane
            :label="$t('serviceTraffic.byRoute')"
            name="routes"
        /></el-tabs>
        <el-table :data="rows" size="default">
          <el-table-column
            :label="
              grouping === 'sources' ? $t('serviceTraffic.source') : $t('serviceTraffic.route')
            "
            min-width="210"
            show-overflow-tooltip
            ><template #default="{ row }">{{
              grouping === 'sources' ? $t('serviceTraffic.sources.' + row.source) : row.route
            }}</template></el-table-column
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
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import api from '../../utils/api'
const { locale } = useI18n()
const snapshot = ref(null)
const loading = ref(false)
const failed = ref(false)
const grouping = ref('sources')
const rows = computed(() => snapshot.value?.[grouping.value] || [])
const chartMax = computed(() =>
  Math.max(1, ...(snapshot.value?.history || []).map((point) => point.requests))
)
let controller = null
let poll = null
let disposed = false
async function refresh() {
  if (loading.value || document.hidden || disposed) return
  loading.value = true
  controller = new AbortController()
  try {
    const response = await api.get('/admin/service-traffic', { signal: controller.signal })
    if (!disposed) {
      snapshot.value = response.data
      failed.value = false
    }
  } catch (error) {
    if (error.code !== 'ERR_CANCELED' && !disposed) failed.value = true
  } finally {
    if (!disposed) loading.value = false
  }
}
const visible = () => {
  if (!document.hidden) refresh()
}
onMounted(() => {
  refresh()
  poll = setInterval(refresh, 15000)
  document.addEventListener('visibilitychange', visible)
})
onBeforeUnmount(() => {
  disposed = true
  clearInterval(poll)
  controller?.abort()
  document.removeEventListener('visibilitychange', visible)
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
.page-heading,
.panel-heading,
.traffic-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
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
}
.traffic-minute span {
  position: absolute;
  bottom: 0;
  width: 100%;
  background: var(--el-color-primary);
}
.traffic-minute .errors {
  background: var(--el-color-danger);
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
</style>
