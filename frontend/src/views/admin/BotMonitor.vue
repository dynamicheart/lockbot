<template>
  <div>
    <!-- Stats panel (toggleable) -->
    <div class="stats-toggle" @click="showStats = !showStats">
      <el-icon><TrendCharts /></el-icon>
      <span>{{ $t('botList.stats') }}</span>
      <el-icon class="stats-arrow" :class="{ expanded: showStats }"><ArrowDown /></el-icon>
    </div>
    <el-collapse-transition>
      <el-row v-show="showStats" :gutter="16" class="stats-row">
        <el-col :xs="12" :sm="6">
          <div class="stat-card" @click="statusFilter = ''">
            <div class="stat-value primary">{{ stats.total }}</div>
            <div class="stat-label">{{ $t('dashboard.totalBots') }}</div>
          </div>
        </el-col>
        <el-col :xs="12" :sm="6">
          <div class="stat-card" @click="statusFilter = 'running'">
            <div class="stat-value success">{{ stats.running }}</div>
            <div class="stat-label">{{ $t('dashboard.running') }}</div>
          </div>
        </el-col>
        <el-col :xs="12" :sm="6">
          <div class="stat-card" @click="statusFilter = 'stopped'">
            <div class="stat-value muted">{{ stats.stopped }}</div>
            <div class="stat-label">{{ $t('dashboard.stopped') }}</div>
          </div>
        </el-col>
        <el-col :xs="12" :sm="6">
          <div class="stat-card" @click="statusFilter = 'error'">
            <div class="stat-value danger">{{ stats.error }}</div>
            <div class="stat-label">{{ $t('dashboard.error') }}</div>
          </div>
        </el-col>
      </el-row>
    </el-collapse-transition>

    <!-- Toolbar -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="search"
          :placeholder="$t('botList.searchPlaceholder')"
          clearable
          :prefix-icon="Search"
          style="width: 220px"
        />
        <el-select
          v-model="ownerFilter"
          :placeholder="$t('botList.filterByOwner')"
          clearable
          filterable
          style="width: 160px"
        >
          <el-option v-for="owner in ownerOptions" :key="owner" :label="owner" :value="owner" />
        </el-select>
        <el-select
          v-model="statusFilter"
          :placeholder="$t('botList.filterByStatus')"
          clearable
          style="width: 150px"
        >
          <el-option :label="$t('status.running')" value="running" />
          <el-option :label="$t('status.stopped')" value="stopped" />
          <el-option :label="$t('status.error')" value="error" />
        </el-select>
        <el-select
          v-model="typeFilter"
          :placeholder="$t('botList.filterByType')"
          clearable
          style="width: 140px"
        >
          <el-option label="NODE" value="NODE" />
          <el-option label="DEVICE" value="DEVICE" />
          <el-option label="QUEUE" value="QUEUE" />
        </el-select>
        <el-button v-if="authStore?.isSuperAdmin" @click="handleBackup">
          <el-icon><Download /></el-icon> {{ $t('admin.backupDatabase') }}
        </el-button>
        <el-button v-if="authStore?.isSuperAdmin" @click="handleDownloadStates">
          <el-icon><Download /></el-icon> {{ $t('admin.downloadAllStates') }}
        </el-button>
      </div>
    </div>

    <!-- Table -->
    <el-skeleton :loading="loading" :rows="6" animated>
      <template #default>
        <el-empty v-if="filtered.length === 0" :description="$t('common.noData')" />
        <el-table
          v-else
          :data="filtered"
          stripe
          class="bot-table"
          @row-click="(row) => $router.push(`/bots/${row.id}`)"
        >
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column
            prop="name"
            :label="$t('admin.botName')"
            min-width="140"
            show-overflow-tooltip
          />
          <el-table-column :label="$t('botDetail.type')" width="160">
            <template #default="{ row }">
              <el-tag size="small" type="primary" effect="plain">{{ row.bot_type }}</el-tag>
              <el-tag size="small" type="info" effect="plain">{{ row.platform }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="status" :label="$t('botDetail.status')" width="110">
            <template #default="{ row }">
              <StatusBadge :status="row.status" />
            </template>
          </el-table-column>
          <el-table-column prop="owner" :label="$t('admin.owner')" width="120" />
          <el-table-column :label="$t('botCard.group')" width="180">
            <template #default="{ row }">
              <template v-if="row.group_id">
                <el-tag
                  v-for="gid in row.group_id.split(',').slice(0, 2)"
                  :key="gid"
                  size="small"
                  effect="plain"
                  class="group-tag"
                  @click.stop="copyText(gid)"
                  >{{ gid }}</el-tag
                >
                <el-tag
                  v-if="row.group_id.split(',').length > 2"
                  size="small"
                  type="info"
                  effect="plain"
                  >+{{ row.group_id.split(',').length - 2 }}</el-tag
                >
              </template>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column :label="$t('botCard.resources')" width="120">
            <template #default="{ row }">
              <span class="list-stat">{{ getResourceLabel(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="$t('botCard.lastActive')" width="110">
            <template #default="{ row }">{{
              formatRelativeTime(row.last_request_at) || '-'
            }}</template>
          </el-table-column>
          <el-table-column prop="created_at" :label="$t('botCard.created')" width="130">
            <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
          </el-table-column>
        </el-table>
      </template>
    </el-skeleton>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import api from '../../utils/api'
import StatusBadge from '../../components/StatusBadge.vue'
import { Search, Download } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '../../stores/auth'
import { useHelpers } from '../../utils/helpers'

const { t } = useI18n()
const authStore = useAuthStore()
const { formatDate, formatRelativeTime, copyText } = useHelpers()
const allBots = ref([])
const loading = ref(false)
const search = ref('')
const statusFilter = ref('')
const typeFilter = ref('')
const ownerFilter = ref('')
const showStats = ref(true)

onMounted(async () => {
  loading.value = true
  try {
    const res = await api.get('/admin/bots')
    allBots.value = res.data
  } catch {
    allBots.value = []
  } finally {
    loading.value = false
  }
})

const stats = computed(() => {
  return {
    total: allBots.value.length,
    running: allBots.value.filter((b) => b.status === 'running').length,
    stopped: allBots.value.filter((b) => b.status === 'stopped').length,
    error: allBots.value.filter((b) => b.status === 'error').length,
  }
})

const ownerOptions = computed(() => {
  const owners = [...new Set(allBots.value.map((b) => b.owner).filter(Boolean))]
  return owners.sort()
})

const filtered = computed(() => {
  const q = search.value.toLowerCase()
  return allBots.value.filter((b) => {
    if (q && !b.name.toLowerCase().includes(q) && !(b.owner || '').toLowerCase().includes(q))
      return false
    if (ownerFilter.value && b.owner !== ownerFilter.value) return false
    if (statusFilter.value && b.status !== statusFilter.value) return false
    if (typeFilter.value && b.bot_type !== typeFilter.value) return false
    return true
  })
})

function getResourceLabel(row) {
  try {
    const configs =
      typeof row.cluster_configs === 'string'
        ? JSON.parse(row.cluster_configs)
        : row.cluster_configs || {}
    const nodes = Object.keys(configs).length
    if (row.bot_type === 'DEVICE') {
      let totalDevices = 0
      for (const devices of Object.values(configs)) {
        totalDevices += Array.isArray(devices) ? devices.length : 0
      }
      return `${nodes}n / ${totalDevices}d`
    }
    return `${nodes} nodes`
  } catch {
    return '-'
  }
}

async function handleBackup() {
  try {
    const res = await api.get('/admin/backup', { responseType: 'blob' })
    const url = URL.createObjectURL(res.data)
    const a = document.createElement('a')
    a.href = url
    const cd = res.headers['content-disposition']
    const match = cd?.match(/filename=(.+)/)
    a.download = match ? match[1] : 'lockbot_backup.db'
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    ElMessage.error(t('common.error'))
  }
}

async function handleDownloadStates() {
  try {
    const res = await api.get('/admin/bot-states', { responseType: 'blob' })
    const url = URL.createObjectURL(res.data)
    const a = document.createElement('a')
    a.href = url
    const cd = res.headers['content-disposition']
    const match = cd?.match(/filename="?([^";]+)"?/)
    a.download = match ? decodeURIComponent(match[1]) : 'lockbot_states.zip'
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    ElMessage.error(t('common.error'))
  }
}
</script>

<style scoped>
.group-tag {
  cursor: pointer;
  margin-right: 4px;
  margin-bottom: 2px;
}
.stats-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  color: var(--lb-text-secondary);
  cursor: pointer;
  user-select: none;
  margin-bottom: 12px;
}
.stats-toggle:hover {
  color: var(--lb-color-primary);
}
.stats-arrow {
  transition: transform 0.3s;
}
.stats-arrow.expanded {
  transform: rotate(180deg);
}
.stats-row {
  margin-bottom: 20px;
}
.stat-card {
  text-align: center;
  padding: 16px;
  border-radius: 8px;
  background: var(--lb-bg-card);
  border: 1px solid var(--lb-border-light);
  cursor: pointer;
  transition:
    border-color 0.2s,
    box-shadow 0.2s;
}
.stat-card:hover {
  border-color: var(--lb-color-primary);
  box-shadow: var(--lb-shadow-card-hover);
}
.stat-value {
  font-size: 28px;
  font-weight: 700;
}
.stat-value.primary {
  color: var(--lb-color-primary);
}
.stat-value.success {
  color: var(--lb-color-success);
}
.stat-value.muted {
  color: var(--lb-text-secondary);
}
.stat-value.danger {
  color: var(--lb-color-danger);
}
.stat-label {
  font-size: 13px;
  color: var(--lb-text-secondary);
  margin-top: 4px;
}
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 20px;
}
.toolbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.bot-table :deep(tr) {
  cursor: pointer;
}
.list-stat {
  font-size: 13px;
  color: var(--lb-text-regular);
}
</style>
