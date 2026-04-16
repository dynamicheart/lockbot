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
        {{ userMode ? $t('nav.myActivity') : $t('audit.title') }}
        <span style="font-size: 14px; font-weight: 400; color: var(--lb-text-secondary)"
          >({{ total }})</span
        >
      </h2>
      <el-button @click="fetchLogs">
        <el-icon><Refresh /></el-icon> {{ $t('common.refresh') }}
      </el-button>
    </div>

    <!-- Security alert banner (userMode only) -->
    <el-alert
      v-if="userMode && failedLoginIps.length"
      :title="$t('audit.securityAlertTitle')"
      type="warning"
      show-icon
      :closable="false"
      style="margin-bottom: 16px"
    >
      <template #default>
        {{
          $t('audit.securityAlertDesc', { count: failedLoginCount, ips: failedLoginIps.join(', ') })
        }}
      </template>
    </el-alert>

    <!-- Filters -->
    <el-card style="margin-bottom: 16px">
      <el-row :gutter="12">
        <el-col :xs="24" :sm="8" :md="userMode ? 8 : 6">
          <el-select
            v-model="filters.action"
            :placeholder="$t('audit.allActions')"
            clearable
            style="width: 100%"
            @change="onFilterChange"
          >
            <el-option
              v-for="(label, key) in visibleActionOptions"
              :key="key"
              :label="label"
              :value="key"
            />
          </el-select>
        </el-col>
        <el-col :xs="24" :sm="8" :md="userMode ? 8 : 5">
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
        <el-col v-if="!userMode" :xs="24" :sm="8" :md="6">
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
        <el-col :xs="24" :sm="12" :md="userMode ? 8 : 7">
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

        <el-table-column v-if="!userMode" :label="$t('audit.operator')" min-width="120">
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
              <router-link
                v-if="row.target_type === 'bot' && row.target_id"
                :to="`/bots/${row.target_id}`"
                style="color: var(--el-color-primary); text-decoration: none"
              >
                {{ row.target_name }}
              </router-link>
              <span v-else>{{ row.target_name }}</span>
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

        <!-- Empty state -->
        <template #empty>
          <div style="padding: 40px 0; color: var(--lb-text-secondary)">
            <el-icon style="font-size: 40px; display: block; margin: 0 auto 12px"
              ><Document
            /></el-icon>
            {{ userMode ? $t('audit.noActivity') : $t('audit.noData') }}
          </div>
        </template>
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
import { Refresh, InfoFilled, User, Document } from '@element-plus/icons-vue'
import { useAuthStore } from '../../stores/auth'
import api from '../../utils/api'

const props = defineProps({
  userMode: { type: Boolean, default: false },
})

const { tm } = useI18n()
const authStore = useAuthStore()

const loading = ref(false)
const logs = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)

// Failed-login security alert state (userMode only)
const failedLoginCount = ref(0)
const failedLoginIps = ref([])

const filters = ref({
  action: '',
  result: '',
  operator: '',
})
const dateRange = ref(null)

const actionOptions = computed(() => tm('audit.actions'))

// In userMode show only auth.* and bot.* actions (relevant to regular users)
const visibleActionOptions = computed(() => {
  const all = actionOptions.value
  if (!props.userMode) return all
  return Object.fromEntries(
    Object.entries(all).filter(([k]) => k.startsWith('auth.') || k.startsWith('bot.'))
  )
})

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
    if (!props.userMode && filters.value.operator) params.operator_username = filters.value.operator
    if (props.userMode && authStore.user?.id) params.operator_id = authStore.user.id
    if (dateRange.value?.[0]) params.start_date = dateRange.value[0].toISOString()
    if (dateRange.value?.[1]) params.end_date = dateRange.value[1].toISOString()

    const res = await api.get('/audit/logs', { params })
    logs.value = res.data.items
    total.value = res.data.total
  } finally {
    loading.value = false
  }
}

// Fetch recent failed login attempts for the security banner (userMode only)
async function fetchSecurityAlerts() {
  if (!props.userMode) return
  try {
    const res = await api.get('/audit/logs', {
      params: { action: 'auth.login', result: 'failure', limit: 50 },
    })
    const failures = res.data.items
    failedLoginCount.value = failures.length
    const ips = [...new Set(failures.map((f) => f.ip_address).filter(Boolean))]
    failedLoginIps.value = ips
  } catch {
    // non-blocking
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

onMounted(() => {
  fetchLogs()
  fetchSecurityAlerts()
})
</script>
