<template>
  <div>
    <div
      style="
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 20px;
      "
    >
      <h2 style="margin: 0">
        {{ $t('audit.title') }}
        <span style="font-size: 14px; font-weight: 400; color: var(--lb-text-secondary)"
          >({{ total }})</span
        >
      </h2>
      <el-button @click="fetchLogs">
        <el-icon><Refresh /></el-icon> {{ $t('common.refresh') }}
      </el-button>
    </div>

    <!-- Filters -->
    <el-card style="margin-bottom: 16px">
      <el-row :gutter="12">
        <el-col :xs="24" :sm="8" :md="6">
          <el-select
            v-model="filters.action"
            :placeholder="$t('audit.allActions')"
            clearable
            style="width: 100%"
            @change="onFilterChange"
          >
            <el-option
              v-for="(label, key) in actionOptions"
              :key="key"
              :label="label"
              :value="key"
            />
          </el-select>
        </el-col>
        <el-col :xs="24" :sm="8" :md="5">
          <el-select
            v-model="filters.result"
            :placeholder="$t('audit.allResults')"
            clearable
            style="width: 100%"
            @change="onFilterChange"
          >
            <el-option :label="$t('audit.success')" value="success" />
            <el-option :label="$t('audit.failure')" value="failure" />
          </el-select>
        </el-col>
        <el-col :xs="24" :sm="8" :md="6">
          <el-input
            v-model="filters.operator"
            :placeholder="$t('audit.filterByOperator')"
            clearable
            style="width: 100%"
            @change="onFilterChange"
            @clear="onFilterChange"
          >
            <template #prefix
              ><el-icon><User /></el-icon
            ></template>
          </el-input>
        </el-col>
        <el-col :xs="24" :sm="12" :md="7">
          <el-date-picker
            v-model="dateRange"
            type="datetimerange"
            :range-separator="'~'"
            :start-placeholder="$t('audit.startDate')"
            :end-placeholder="$t('audit.endDate')"
            style="width: 100%"
            @change="onFilterChange"
          />
        </el-col>
      </el-row>
    </el-card>

    <!-- Table -->
    <el-card>
      <el-table v-loading="loading" :data="logs" stripe>
        <el-table-column :label="$t('audit.time')" width="180" fixed>
          <template #default="{ row }">
            <span style="font-size: 12px; color: var(--lb-text-secondary)">
              {{ formatTime(row.created_at) }}
            </span>
          </template>
        </el-table-column>

        <el-table-column :label="$t('audit.operator')" min-width="120">
          <template #default="{ row }">
            <div>
              <span style="font-weight: 500">{{ row.operator_username }}</span>
              <el-tag
                :type="roleTagType(row.operator_role)"
                size="small"
                effect="plain"
                style="margin-left: 6px"
              >
                {{ translateRole(row.operator_role) }}
              </el-tag>
            </div>
          </template>
        </el-table-column>

        <el-table-column :label="$t('audit.action')" width="150">
          <template #default="{ row }">
            <el-tag size="small" :type="actionTagType(row.action)">
              {{ translateAction(row.action) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="$t('audit.target')" min-width="140">
          <template #default="{ row }">
            <span v-if="row.target_name">
              <el-tag size="small" effect="plain" style="margin-right: 4px">
                {{ row.target_type }}
              </el-tag>
              {{ row.target_name }}
              <span style="color: var(--lb-text-muted); font-size: 12px">
                #{{ row.target_id }}</span
              >
            </span>
            <span v-else style="color: var(--lb-text-muted)">—</span>
          </template>
        </el-table-column>

        <el-table-column :label="$t('audit.result')" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="row.result === 'success' ? 'success' : 'danger'" size="small">
              {{ row.result === 'success' ? $t('audit.success') : $t('audit.failure') }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="$t('audit.ip')" width="130">
          <template #default="{ row }">
            <span style="font-size: 12px; color: var(--lb-text-secondary)">
              {{ row.ip_address || '—' }}
            </span>
          </template>
        </el-table-column>

        <el-table-column :label="$t('audit.detail')" min-width="140">
          <template #default="{ row }">
            <span v-if="row.detail" style="font-size: 12px; color: var(--lb-text-secondary)">
              <el-popover placement="left" :width="300" trigger="hover">
                <template #reference>
                  <el-link type="info" :underline="false">
                    <el-icon><InfoFilled /></el-icon>
                  </el-link>
                </template>
                <pre
                  style="margin: 0; font-size: 12px; white-space: pre-wrap; word-break: break-all"
                  >{{ formatDetail(row.detail) }}</pre
                >
              </el-popover>
            </span>
            <span v-else style="color: var(--lb-text-muted)">—</span>
          </template>
        </el-table-column>
      </el-table>

      <div style="display: flex; justify-content: flex-end; margin-top: 16px">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @change="fetchLogs"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { Refresh, InfoFilled, User } from '@element-plus/icons-vue'
import api from '../../utils/api'

const { tm } = useI18n()

const loading = ref(false)
const logs = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)

const filters = ref({
  action: '',
  result: '',
  operator: '',
})
const dateRange = ref(null)

const actionOptions = computed(() => tm('audit.actions'))

function onFilterChange() {
  page.value = 1
  fetchLogs()
}

async function fetchLogs() {
  loading.value = true
  try {
    const params = {
      page: page.value,
      limit: pageSize.value,
    }
    if (filters.value.action) params.action = filters.value.action
    if (filters.value.result) params.result = filters.value.result
    if (filters.value.operator) params.operator_username = filters.value.operator
    if (dateRange.value?.[0]) params.start_date = dateRange.value[0].toISOString()
    if (dateRange.value?.[1]) params.end_date = dateRange.value[1].toISOString()

    const res = await api.get('/audit/logs', { params })
    logs.value = res.data.items
    total.value = res.data.total
  } finally {
    loading.value = false
  }
}

function formatTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

function formatDetail(detail) {
  if (typeof detail === 'object') return JSON.stringify(detail, null, 2)
  return detail
}

function roleTagType(role) {
  if (role === 'super_admin') return 'danger'
  if (role === 'admin') return 'warning'
  return 'info'
}

function translateRole(role) {
  return tm('audit.roles')?.[role] || role
}

function translateAction(action) {
  return tm('audit.actions')?.[action] || action
}

function actionTagType(action) {
  if (action?.startsWith('auth.')) return ''
  if (action?.startsWith('bot.delete') || action?.startsWith('user.force_logout')) return 'danger'
  if (action?.startsWith('bot.') || action?.startsWith('user.')) return 'primary'
  if (action?.startsWith('admin.')) return 'warning'
  return 'info'
}

onMounted(fetchLogs)
</script>
