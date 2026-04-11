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
        <el-input v-model="search" :placeholder="$t('botList.searchPlaceholder')" clearable :prefix-icon="Search" style="width: 220px" />
        <el-select v-model="statusFilter" :placeholder="$t('botList.filterByStatus')" clearable style="width: 150px">
          <el-option :label="$t('status.running')" value="running" />
          <el-option :label="$t('status.stopped')" value="stopped" />
          <el-option :label="$t('status.error')" value="error" />
        </el-select>
        <el-select v-model="typeFilter" :placeholder="$t('botList.filterByType')" clearable style="width: 140px">
          <el-option label="NODE" value="NODE" />
          <el-option label="DEVICE" value="DEVICE" />
          <el-option label="QUEUE" value="QUEUE" />
        </el-select>
      </div>
      <div class="toolbar-right">
        <!-- View toggle -->
        <el-button-group>
          <el-button :type="viewMode === 'card' ? 'primary' : ''" text @click="viewMode = 'card'">
            <el-icon><Grid /></el-icon>
          </el-button>
          <el-button :type="viewMode === 'list' ? 'primary' : ''" text @click="viewMode = 'list'">
            <el-icon><List /></el-icon>
          </el-button>
        </el-button-group>
        <el-button type="primary" @click="$router.push('/bots/create')">
          <el-icon><Plus /></el-icon> {{ $t('nav.createBot') }}
        </el-button>
      </div>
    </div>

    <!-- Content -->
    <el-skeleton :loading="botsStore.loading" :rows="6" animated>
      <template #default>
        <el-empty v-if="filtered.length === 0" :description="$t('common.noData')" />

        <!-- Card view -->
        <el-row v-else-if="viewMode === 'card'" :gutter="16">
          <el-col v-for="bot in filtered" :key="bot.id" :xs="24" :sm="12" :md="8" :lg="6" style="margin-bottom: 16px">
            <BotCard :bot="bot" />
          </el-col>
        </el-row>

        <!-- List view -->
        <el-table v-else :data="filtered" stripe @row-click="row => $router.push(`/bots/${row.id}`)" class="bot-table">
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="name" :label="$t('admin.botName')" min-width="140" show-overflow-tooltip />
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
          <el-table-column :label="$t('botCard.group')" width="180">
            <template #default="{ row }">
              <template v-if="row.group_id">
                <el-tag v-for="gid in row.group_id.split(',').slice(0, 2)" :key="gid" size="small" effect="plain" class="group-tag" @click="copyText(gid)">{{ gid }}</el-tag>
                <el-tag v-if="row.group_id.split(',').length > 2" size="small" type="info" effect="plain">+{{ row.group_id.split(',').length - 2 }}</el-tag>
              </template>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column :label="$t('botCard.resources')" width="120">
            <template #default="{ row }">
              <span class="list-stat">{{ getResourceLabel(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="$t('botCard.usage')" width="140">
            <template #default="{ row }">
              <template v-if="getBotStats(row).utilization">
                <div class="list-usage-bar-container">
                  <div class="list-usage-bar">
                    <div class="usage-bar-fill idle" :style="{ width: getIdlePercent(row) + '%' }"></div>
                    <div class="usage-bar-fill inuse" :style="{ width: getInUsePercent(row) + '%' }"></div>
                  </div>
                  <span class="usage-label">{{ getUsageText(row) }}</span>
                </div>
              </template>
              <span v-else class="usage-label">-</span>
            </template>
          </el-table-column>
          <el-table-column :label="$t('botCard.lastActive')" width="110">
            <template #default="{ row }">{{ formatRelativeTime(row.last_request_at) || '-' }}</template>
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
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useBotsStore } from '../stores/bots'
import BotCard from '../components/BotCard.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useHelpers } from '../utils/helpers'

const VIEW_KEY = 'lockbot_view_mode'
const STATS_KEY = 'lockbot_show_stats'

const { t } = useI18n()
const { formatDate, formatRelativeTime, copyText } = useHelpers()
const router = useRouter()
const botsStore = useBotsStore()
const search = ref('')
const statusFilter = ref('')
const typeFilter = ref('')
const viewMode = ref(localStorage.getItem(VIEW_KEY) || 'card')
const showStats = ref(localStorage.getItem(STATS_KEY) !== 'false')
let stateRefreshTimer = null

// Persist preferences
watch(viewMode, v => localStorage.setItem(VIEW_KEY, v))
watch(showStats, v => localStorage.setItem(STATS_KEY, v))

onMounted(() => {
  botsStore.fetchBots()
  botsStore.fetchRunningStates()
  stateRefreshTimer = setInterval(() => {
    botsStore.fetchRunningStates()
  }, 15000)
})

onUnmounted(() => {
  clearInterval(stateRefreshTimer)
})

const stats = computed(() => {
  const all = botsStore.bots
  return {
    total: all.length,
    running: all.filter(b => b.status === 'running').length,
    stopped: all.filter(b => b.status === 'stopped').length,
    error: all.filter(b => b.status === 'error').length,
  }
})

const filtered = computed(() => {
  return botsStore.bots.filter(b => {
    if (search.value && !b.name.toLowerCase().includes(search.value.toLowerCase())) return false
    if (statusFilter.value && b.status !== statusFilter.value) return false
    if (typeFilter.value && b.bot_type !== typeFilter.value) return false
    return true
  })
})

function getBotStats(row) {
  return botsStore.computeBotStats(row)
}

function getResourceLabel(row) {
  const rc = getBotStats(row).resourceCounts
  if (row.bot_type === 'DEVICE') return `${rc.nodes}n / ${rc.devices}d`
  return `${rc.nodes} nodes`
}

function getInUsePercent(row) {
  const u = getBotStats(row).utilization
  if (!u || u.total === 0) return 0
  return Math.round((u.inUse / u.total) * 100)
}

function getIdlePercent(row) {
  const u = getBotStats(row).utilization
  if (!u || u.total === 0) return 100
  return Math.round((u.idle / u.total) * 100)
}

function getUsageText(row) {
  const u = getBotStats(row).utilization
  if (!u) return '-'
  return `${u.inUse}/${u.total}`
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
  transition: border-color 0.2s, box-shadow 0.2s;
}
.stat-card:hover {
  border-color: var(--lb-color-primary);
  box-shadow: var(--lb-shadow-card-hover);
}
.stat-value {
  font-size: 28px;
  font-weight: 700;
}
.stat-value.primary { color: var(--lb-color-primary); }
.stat-value.success { color: var(--lb-color-success); }
.stat-value.muted { color: var(--lb-text-secondary); }
.stat-value.danger { color: var(--lb-color-danger); }
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
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.bot-table {
  cursor: pointer;
}
.bot-table :deep(tr) {
  cursor: pointer;
}
.list-stat {
  font-size: 13px;
  color: var(--lb-text-regular);
}
.list-usage-bar-container {
  display: flex;
  align-items: center;
  gap: 6px;
}
.list-usage-bar {
  flex: 1;
  height: 6px;
  border-radius: 3px;
  background: var(--el-fill-color-light);
  overflow: hidden;
  display: flex;
}
.list-usage-bar .usage-bar-fill.idle {
  background: var(--el-color-info-light-5);
  transition: width 0.3s;
}
.list-usage-bar .usage-bar-fill.inuse {
  background: var(--el-color-success);
  transition: width 0.3s;
}
.usage-label {
  font-size: 11px;
  white-space: nowrap;
}
</style>
