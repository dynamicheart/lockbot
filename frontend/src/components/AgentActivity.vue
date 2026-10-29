<template>
  <section class="agent-activity" :aria-label="t('agentActivity.title')">
    <div class="activity-toolbar">
      <div>
        <h3>{{ t('agentActivity.title') }}</h3>
        <p>{{ t('agentActivity.description') }}</p>
      </div>
      <el-button size="small" :loading="loading" @click="refresh">
        <el-icon><Refresh /></el-icon> {{ t('common.refresh') }}
      </el-button>
    </div>
    <el-alert
      v-if="error"
      :title="t('agentActivity.error')"
      type="error"
      :closable="false"
      show-icon
      class="activity-error"
    />
    <div v-loading="loading" :aria-busy="loading">
      <el-radio-group
        v-model="status"
        size="small"
        style="margin-bottom: 16px"
        @change="changeFilter"
      >
        <el-radio-button value="active">{{ t('agentActivity.active') }}</el-radio-button>
        <el-radio-button value="history">{{ t('agentActivity.history') }}</el-radio-button>
        <el-radio-button value="all">{{ t('agentActivity.all') }}</el-radio-button>
      </el-radio-group>
      <el-table
        :data="items"
        :empty-text="t('agentActivity.empty')"
        row-key="row_key"
        class="activity-table"
      >
        <el-table-column :label="t('agentActivity.user')" min-width="230">
          <template #default="{ row }">
            <div class="actor-name">{{ row.external_user_id }}</div>
            <div class="actor-note">
              <span>{{ row.platform }}</span>
              <el-tooltip :content="t('agentActivity.labelHelp')" placement="top">
                <span class="authorization-label">{{ row.label || '—' }}</span>
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('agentActivity.authorization')" min-width="110">
          <template #default="{ row }">
            <el-tag :type="row.active ? 'primary' : 'info'" size="small" effect="plain">
              {{
                t(
                  row.active
                    ? 'agentActivity.active'
                    : row.revoked
                      ? 'agentActivity.revoked'
                      : 'agentActivity.expired'
                )
              }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          v-for="metric in metrics"
          :key="metric.key"
          :label="t(`agentActivity.${metric.label}`)"
          align="right"
          min-width="88"
        >
          <template #default="{ row }">
            <span
              class="request-count"
              :class="{
                'count-zero': !row[metric.key],
                'count-warning': metric.warning && row[metric.key] > 0,
              }"
              >{{ number(row[metric.key]) }}</span
            >
          </template>
        </el-table-column>
        <el-table-column :label="t('agentActivity.lastSeen')" min-width="185" align="right">
          <template #default="{ row }">
            <time class="last-seen">{{ formatActivityTime(row.last_seen_at, locale) }}</time>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <el-pagination
      v-if="total > 20"
      v-model:current-page="page"
      :page-size="20"
      :total="total"
      layout="prev, pager, next"
      style="margin-top: 12px"
      @current-change="refresh"
    />
    <div class="activity-footer">
      <span>{{ t('agentActivity.scope', { count: total }) }}</span>
      <el-tooltip :content="t('agentActivity.countingHelp')" placement="top">
        <span class="counting-help" tabindex="0">
          <el-icon><InfoFilled /></el-icon> {{ t('agentActivity.counting') }}
        </span>
      </el-tooltip>
    </div>
  </section>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Refresh, InfoFilled } from '@element-plus/icons-vue'
import api from '../utils/api'
import { formatActivityTime } from '../utils/activityDisplay'

const props = defineProps({ botId: { type: Number, required: true } })
const emit = defineEmits(['updated'])
const { t, locale } = useI18n()
const items = ref([])
const loading = ref(false)
const error = ref(false)
const metrics = [
  { key: 'read_requests', label: 'reads' },
  { key: 'write_requests', label: 'writes' },
  { key: 'refused_requests', label: 'refused', warning: true },
  { key: 'limited_requests', label: 'limited', warning: true },
]
const status = ref('active')
const page = ref(1)
const total = ref(0)
function changeFilter() {
  page.value = 1
  refresh()
}
const number = (value) => new Intl.NumberFormat(locale.value).format(value || 0)
let generation = 0
async function refresh() {
  const current = ++generation
  loading.value = true
  error.value = false
  try {
    const { data } = await api.get(`/bots/${props.botId}/agent-activity`, {
      params: { status: status.value, page: page.value, limit: 20 },
    })
    if (current === generation) {
      items.value = data.items
      total.value = data.total
      emit('updated')
    }
  } catch {
    if (current === generation) error.value = true
  } finally {
    if (current === generation) loading.value = false
  }
}
onMounted(refresh)
watch(
  () => props.botId,
  () => {
    items.value = []
    total.value = 0
    page.value = 1
    refresh()
  }
)
</script>

<style scoped>
.activity-toolbar,
.activity-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
}
.activity-toolbar {
  margin: 6px 0 20px;
}
.activity-toolbar h3 {
  margin: 0 0 6px;
  color: var(--lb-text-primary);
  font-size: 16px;
  font-weight: 600;
}
.activity-toolbar p,
.activity-footer {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--lb-text-regular);
}
.activity-error {
  margin-bottom: 16px;
}
.activity-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  background: var(--el-fill-color-light);
  border: 1px solid var(--lb-border-color);
  border-radius: 8px;
  margin-bottom: 18px;
  padding: 16px 0;
}
.activity-metric {
  padding: 0 20px;
  border-right: 1px solid var(--lb-border-color);
}
.activity-metric:last-child {
  border-right: 0;
}
.metric-label {
  display: block;
  font-size: 12px;
  color: var(--lb-text-regular);
  margin-bottom: 6px;
}
.activity-metric strong {
  color: var(--lb-text-primary);
  font-size: 26px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.activity-metric .metric-warning,
.count-warning {
  color: var(--el-color-warning-dark-2);
}
.activity-table {
  width: 100%;
  --el-table-header-bg-color: var(--el-fill-color-light);
}
.activity-table :deep(.el-table__cell) {
  padding: 12px 0;
}
.activity-table :deep(th.el-table__cell) {
  font-size: 12px;
  font-weight: 500;
}
.actor-name {
  color: var(--lb-text-primary);
  font-weight: 500;
  overflow-wrap: anywhere;
}
.actor-note {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  margin-top: 3px;
  color: var(--lb-text-regular);
  font-size: 12px;
}
.authorization-label {
  overflow-wrap: anywhere;
}
.request-count,
.last-seen {
  font-variant-numeric: tabular-nums;
  font-size: 13px;
}
.count-zero {
  color: var(--lb-text-secondary);
}
.last-seen {
  color: var(--lb-text-regular);
  white-space: nowrap;
}
.activity-footer {
  margin-top: 14px;
  flex-wrap: wrap;
}
.counting-help {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  cursor: help;
}
@media (max-width: 640px) {
  .activity-toolbar {
    align-items: flex-start;
  }
  .activity-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    row-gap: 20px;
  }
  .activity-metric:nth-child(2) {
    border-right: 0;
  }
}
</style>
