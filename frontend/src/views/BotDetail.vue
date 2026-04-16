<template>
  <div v-loading="loading">
    <div class="detail-header">
      <div class="detail-header-left">
        <el-button text @click="$router.back()">
          <el-icon><ArrowLeft /></el-icon> {{ $t('botDetail.back') }}
        </el-button>
        <h2 class="detail-title">{{ bot?.name || 'Bot Detail' }}</h2>
        <StatusBadge v-if="bot" :status="bot.status" />
      </div>
      <div class="detail-header-actions">
        <el-button
          v-if="canOperate && (bot?.status === 'stopped' || bot?.status === 'error')"
          type="success"
          round
          :loading="actionLoading"
          :disabled="actionLoading"
          @click="handleStart"
        >
          <el-icon><VideoPlay /></el-icon> {{ $t('botDetail.start') }}
        </el-button>
        <el-button
          v-if="canOperate && bot?.status === 'running'"
          type="warning"
          round
          :loading="actionLoading"
          :disabled="actionLoading"
          @click="handleStop"
        >
          <el-icon><SwitchButton /></el-icon> {{ $t('botDetail.stop') }}
        </el-button>
        <el-button
          v-if="canEdit"
          type="primary"
          round
          @click="$router.push(`/bots/${bot.id}/edit`)"
        >
          <el-icon><Edit /></el-icon> {{ $t('botDetail.editConfig') }}
        </el-button>
        <el-button
          v-if="canDelete"
          type="danger"
          round
          :loading="actionLoading"
          :disabled="actionLoading"
          @click="handleDelete"
        >
          <el-icon><Delete /></el-icon> {{ $t('common.delete') }}
        </el-button>
      </div>
    </div>

    <div v-if="bot">
      <el-card style="margin-bottom: 20px">
        <template #header
          ><span>{{ $t('botDetail.config') }}</span></template
        >
        <el-descriptions :column="2" border>
          <el-descriptions-item :label="$t('botDetail.botId')">{{ bot.id }}</el-descriptions-item>
          <el-descriptions-item :label="$t('botDetail.type')">
            <el-tag size="small" type="primary" effect="plain">{{ bot.bot_type }}</el-tag>
            <el-tag size="small" type="info" effect="plain">{{ bot.platform }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('botDetail.status')">
            <StatusBadge :status="bot.status" />
          </el-descriptions-item>
          <el-descriptions-item :label="$t('botDetail.groupId')">
            <template v-if="bot.group_id">
              <el-tag
                v-for="gid in bot.group_id.split(',')"
                :key="gid"
                size="small"
                effect="plain"
                class="group-tag"
                @click="copyText(gid)"
                >{{ gid }}</el-tag
              >
            </template>
            <span v-else>-</span>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('botDetail.owner')">
            <div style="display: flex; align-items: center; gap: 8px">
              <span>{{ botOwner }}</span>
              <el-button
                v-if="canTransfer"
                text
                size="small"
                type="primary"
                @click="showTransferOwner = true"
                >{{ $t('botDetail.transferOwner') }}</el-button
              >
            </div>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('botDetail.callbackUrl')" :span="2">
            <div class="secret-field">
              <code>{{ callbackUrl }}</code>
              <el-popover
                v-if="showCallbackHint"
                :visible="true"
                placement="bottom"
                :width="240"
                trigger="manual"
              >
                <template #reference>
                  <el-icon class="secret-icon" @click="copyCallbackUrl"><CopyDocument /></el-icon>
                </template>
                <div class="callback-hint">
                  <div class="callback-hint-title">{{ $t('botCreate.callbackHintTitle') }}</div>
                  <div>{{ $t('botCreate.callbackHintStep1') }}</div>
                  <div>{{ $t('botCreate.callbackHintStep2') }}</div>
                  <div>{{ $t('botCreate.callbackHintStep3') }}</div>
                </div>
              </el-popover>
              <el-icon v-else class="secret-icon" @click="copyText(callbackUrl)"
                ><CopyDocument
              /></el-icon>
            </div>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('botDetail.webhookUrl')" :span="2">
            <div class="secret-field">
              <code>{{ bot.webhook_url_raw || '-' }}</code>
              <el-icon
                v-if="bot.webhook_url_raw"
                class="secret-icon"
                @click="copyText(bot.webhook_url_raw)"
                ><CopyDocument
              /></el-icon>
            </div>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('botCreate.token')" :span="2">
            <div class="secret-field">
              <code>{{ showToken ? bot.token_raw : maskText(bot.token_raw) }}</code>
              <el-icon class="secret-icon" @click="showToken = !showToken"
                ><View v-if="!showToken" /><Hide v-else
              /></el-icon>
              <el-icon class="secret-icon" @click="copyText(bot.token_raw)"
                ><CopyDocument
              /></el-icon>
            </div>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('botCreate.aesKey')" :span="2">
            <div class="secret-field">
              <code>{{ showAesKey ? bot.aes_key_raw : maskText(bot.aes_key_raw) }}</code>
              <el-icon class="secret-icon" @click="showAesKey = !showAesKey"
                ><View v-if="!showAesKey" /><Hide v-else
              /></el-icon>
              <el-icon class="secret-icon" @click="copyText(bot.aes_key_raw)"
                ><CopyDocument
              /></el-icon>
            </div>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('botDetail.createdAt')">{{
            formatDate(bot.created_at)
          }}</el-descriptions-item>
          <el-descriptions-item :label="$t('botDetail.updatedAt')">{{
            formatDate(bot.updated_at)
          }}</el-descriptions-item>
          <el-descriptions-item :label="$t('botDetail.lastActive')">{{
            bot.last_request_at ? formatRelativeTime(bot.last_request_at) : '-'
          }}</el-descriptions-item>
          <el-descriptions-item :label="$t('botDetail.lastOperator')">{{
            bot.last_user_id || '-'
          }}</el-descriptions-item>
          <el-descriptions-item :label="$t('botDetail.botLanguage')">
            <el-radio-group v-model="botLanguage" size="small" @change="handleLanguageChange">
              <el-radio-button value="zh">中文</el-radio-button>
              <el-radio-button value="en">English</el-radio-button>
            </el-radio-group>
          </el-descriptions-item>
        </el-descriptions>
      </el-card>

      <!-- Statistics -->
      <el-card style="margin-bottom: 20px">
        <template #header
          ><span>{{ $t('botDetail.statistics') }}</span></template
        >
        <el-row :gutter="20">
          <el-col :span="8">
            <div class="detail-stat-box">
              <div class="detail-stat-number">{{ activeUserCount }}</div>
              <div class="detail-stat-desc">{{ $t('botDetail.activeUsers') }}</div>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="detail-stat-box">
              <div class="detail-stat-number">{{ totalCommands }}</div>
              <div class="detail-stat-desc">{{ $t('botDetail.totalCommands') }}</div>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="detail-stat-box">
              <div class="detail-stat-number">{{ currentUtilization }}%</div>
              <div class="detail-stat-desc">{{ $t('botDetail.utilization') }}</div>
            </div>
          </el-col>
        </el-row>
        <div v-if="displayedUsers.length > 0" class="active-users-section">
          <div class="active-users-title">{{ $t('botDetail.activeUsers') }}</div>
          <div class="active-users-chips">
            <el-tag
              v-for="user in displayedUsers"
              :key="user"
              size="small"
              effect="plain"
              style="margin: 2px"
            >
              {{ user }}
            </el-tag>
            <el-tag
              v-if="overflowUserCount > 0"
              size="small"
              type="info"
              effect="plain"
              style="margin: 2px"
            >
              +{{ overflowUserCount }}
            </el-tag>
          </div>
        </div>
      </el-card>

      <!-- Cluster State -->
      <el-card style="margin-bottom: 20px">
        <template #header>
          <div style="display: flex; justify-content: space-between; align-items: center">
            <span>{{ $t('botDetail.clusterState') }}</span>
            <div style="display: flex; gap: 8px">
              <el-button
                v-if="canEdit && !editingState && !viewJson"
                size="small"
                :disabled="bot.status === 'running'"
                @click="startEditState"
              >
                <el-icon><Edit /></el-icon> {{ $t('common.edit') }}
              </el-button>
              <el-button v-if="authStore.isAdmin" size="small" @click="downloadState">
                <el-icon><Download /></el-icon> {{ $t('clusterState.download') }}
              </el-button>
              <el-button v-if="!editingState" size="small" @click="viewJson = !viewJson">
                {{ viewJson ? $t('clusterState.visualView') : 'JSON' }}
              </el-button>
              <template v-if="editingState">
                <el-button size="small" type="primary" :loading="stateSaving" @click="saveState">{{
                  $t('common.save')
                }}</el-button>
                <el-button size="small" @click="editingState = false">{{
                  $t('common.cancel')
                }}</el-button>
              </template>
              <!-- <el-switch v-if="bot.status === 'running'" v-model="stateAutoRefresh" :active-text="$t('log.autoRefresh')" size="small" /> -->
            </div>
          </div>
        </template>
        <div v-if="editingState" class="json-editor">
          <pre
            class="json-editor-backdrop"
            aria-hidden="true"
          ><code v-html="highlightedJson"></code></pre>
          <textarea
            v-model="stateText"
            class="json-editor-textarea"
            spellcheck="false"
            @scroll="syncScroll"
          ></textarea>
        </div>
        <template v-else-if="viewJson">
          <pre class="json-viewer"><code v-html="viewJsonHighlighted"></code></pre>
        </template>
        <template v-else>
          <!-- Empty state -->
          <el-empty
            v-if="!parsedState"
            :description="$t('clusterState.noState')"
            :image-size="60"
          />

          <!-- DEVICE bot: card list -->
          <div v-else-if="botType === 'DEVICE'" class="cluster-cards">
            <div v-for="node in parsedState" :key="node.nodeName" class="cluster-node-card">
              <div class="cluster-node-header">
                <span class="cluster-node-name">
                  <el-icon :size="16"><Monitor /></el-icon>
                  {{ node.nodeName }}
                </span>
                <span class="cluster-node-summary">{{
                  $t('clusterState.summary', {
                    total: node.devices.length,
                    inUse: node.devices.filter(
                      (d) => d.status === 'exclusive' || d.status === 'shared'
                    ).length,
                  })
                }}</span>
              </div>
              <div class="cluster-device-list">
                <div v-for="dev in node.devices" :key="dev.devId" class="cluster-device-item">
                  <div class="cluster-device-left">
                    <span
                      class="status-dot"
                      :class="'status-dot--' + (dev.status || 'idle')"
                    ></span>
                    <span class="cluster-device-model">{{ dev.devModel || dev.devId }}</span>
                  </div>
                  <div class="cluster-device-users">
                    <template v-if="dev.currentUsers && dev.currentUsers.length">
                      <el-tag
                        v-for="u in dev.currentUsers"
                        :key="u.user_id || u"
                        size="small"
                        effect="plain"
                        >{{ u.user_id || u.userId || u }}</el-tag
                      >
                    </template>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- NODE / QUEUE bot: card list -->
          <div v-else class="cluster-cards">
            <div v-for="row in parsedState" :key="row.nodeName" class="cluster-node-card">
              <div class="cluster-node-header">
                <span class="cluster-node-name">
                  <el-icon :size="16"><Monitor /></el-icon>
                  {{ row.nodeName }}
                </span>
                <el-tag size="small" :type="statusTagType(row.status)" effect="light">{{
                  $t('clusterState.' + row.status)
                }}</el-tag>
              </div>
              <div class="cluster-node-body">
                <div class="cluster-node-users">
                  <template v-if="row.currentUsers && row.currentUsers.length">
                    <el-tag
                      v-for="u in row.currentUsers"
                      :key="u.user_id || u"
                      size="small"
                      effect="plain"
                      >{{ u.user_id || u.userId || u }}</el-tag
                    >
                  </template>
                  <span v-else class="cluster-empty-text">-</span>
                </div>
                <div v-if="botType === 'QUEUE' && row.bookingCount" class="cluster-node-booking">
                  {{ $t('clusterState.bookingList') }}: {{ row.bookingCount }}
                </div>
              </div>
            </div>
          </div>
        </template>
      </el-card>

      <!-- Logs -->
      <el-card style="margin-bottom: 20px">
        <template #header
          ><span>{{ $t('botDetail.logs') }}</span></template
        >
        <LogViewer :bot-id="bot.id" />
      </el-card>
    </div>

    <!-- Transfer Owner Dialog -->
    <el-dialog
      v-model="showTransferOwner"
      :title="$t('botDetail.transferOwner')"
      width="440px"
      class="transfer-dialog"
    >
      <el-alert
        :title="$t('botDetail.transferOwnerWarning')"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 16px"
      />
      <el-autocomplete
        v-model="transferUsername"
        :fetch-suggestions="searchUsers"
        :placeholder="$t('botDetail.transferOwnerPlaceholder')"
        clearable
        style="width: 100%"
        @select="(item) => (transferUsername = item.value)"
      >
        <template #prefix
          ><el-icon><User /></el-icon
        ></template>
      </el-autocomplete>
      <template #footer>
        <el-button @click="showTransferOwner = false">{{ $t('common.cancel') }}</el-button>
        <el-button
          type="primary"
          :loading="transferLoading"
          :disabled="!transferUsername.trim()"
          @click="handleTransferOwner"
          >{{ $t('common.confirm') }}</el-button
        >
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBotsStore } from '../stores/bots'
import { useAuthStore } from '../stores/auth'
import StatusBadge from '../components/StatusBadge.vue'
import LogViewer from '../components/LogViewer.vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, User, Monitor } from '@element-plus/icons-vue'
import api from '../utils/api'
import { validateBotState } from '../utils/stateValidation'
import { useI18n } from 'vue-i18n'
import { useHelpers } from '../utils/helpers'

const { t } = useI18n()
const {
  formatDateTime,
  formatRelativeTime,
  maskText,
  copyText,
} = useHelpers()
const route = useRoute()
const router = useRouter()
const botsStore = useBotsStore()
const authStore = useAuthStore()

const bot = ref(null)
const loading = ref(false)
const actionLoading = ref(false)
const showTransferOwner = ref(false)
const transferUsername = ref('')
const transferLoading = ref(false)

const cachedUsers = ref([])
const stateAutoRefresh = ref(false)
let stateTimer = null

function onVisibilityChange() {
  if (document.hidden) {
    clearInterval(stateTimer)
    stateTimer = null
  } else if (stateAutoRefresh.value) {
    startStateTimer()
  }
}

function startStateTimer() {
  clearInterval(stateTimer)
  stateTimer = setInterval(() => fetchState(), 60000)
}

watch(stateAutoRefresh, (val) => {
  if (val) startStateTimer()
  else {
    clearInterval(stateTimer)
    stateTimer = null
  }
})
async function fetchUsers() {
  if (cachedUsers.value.length) return
  try {
    const res = await api.get('/admin/users')
    cachedUsers.value = res.data || []
  } catch {
    /* non-admin, no suggestions */
  }
}
function searchUsers(query, cb) {
  fetchUsers()
  const q = query.toLowerCase()
  const results = cachedUsers.value
    .filter((u) => u.username.toLowerCase().includes(q))
    .map((u) => ({ value: u.username, label: `${u.username} (${u.email})` }))
  cb(results)
}

// State editor
const editingState = ref(false)
const viewJson = ref(false)
const stateText = ref('')
const stateData = ref(null)
const stateSaving = ref(false)

// Secret field visibility
const showAesKey = ref(false)
const showToken = ref(false)

// Bot language
const botLanguage = ref('zh')
const botOwner = computed(() => bot.value?.owner || '-')

// Permission rules:
// - super_admin: can do anything (edit/delete/start/stop/transfer)
// - admin: can only VIEW other users' bots, cannot start/stop/edit/delete
// - user: can only operate own bots, no transfer
const canEdit = computed(() => authStore.canEditBot(bot.value))
const canDelete = computed(() => canEdit.value)
const canOperate = computed(() => authStore.canOperateBot(bot.value))
const canTransfer = computed(() => authStore.isSuperAdmin)

// Command logs for statistics
const commandLogs = ref([])

const callbackUrl = computed(() => {
  if (!bot.value) return ''
  const origin = window.location.origin
  return `${origin}/api/bots/webhook/${bot.value.id}`
})

const CALLBACK_COPIED_KEY = 'lockbot_callback_copied'
const callbackCopied = ref(false)

const showCallbackHint = computed(() => {
  if (!bot.value) return false
  if (bot.value.last_request_at) return false
  if (callbackCopied.value) return false
  try {
    const cache = JSON.parse(localStorage.getItem(CALLBACK_COPIED_KEY) || '{}')
    if (cache[bot.value.id]) return false
  } catch {
    /* ignore */
  }
  return true
})

function copyCallbackUrl() {
  copyText(callbackUrl.value)
  callbackCopied.value = true
  try {
    const cache = JSON.parse(localStorage.getItem(CALLBACK_COPIED_KEY) || '{}')
    cache[bot.value.id] = true
    localStorage.setItem(CALLBACK_COPIED_KEY, JSON.stringify(cache))
  } catch {
    /* ignore */
  }
}

const botType = computed(() => bot.value?.bot_type || 'NODE')

const statusTagType = (status) => {
  const map = { idle: 'info', exclusive: 'warning', shared: 'success' }
  return map[status] || 'info'
}

const isValidState = (data) => {
  if (!data || typeof data !== 'object') return false
  const values = Object.values(data)
  if (values.length === 0) return false
  // NODE/QUEUE state has objects with 'status' field
  // DEVICE state has arrays of objects with 'status' field
  const first = values[0]
  if (Array.isArray(first))
    return first.length > 0 && typeof first[0] === 'object' && 'status' in first[0]
  return typeof first === 'object' && first !== null && 'status' in first
}

const parsedState = computed(() => {
  if (!stateData.value || !isValidState(stateData.value)) return null
  const data = stateData.value
  if (botType.value === 'DEVICE') {
    return Object.entries(data).map(([nodeName, devices]) => ({
      nodeName,
      devices: Array.isArray(devices)
        ? devices.map((d) => ({
            devId: d.dev_id,
            devModel: d.dev_model,
            status: d.status,
            currentUsers: d.current_users || [],
          }))
        : [],
    }))
  }
  // NODE / QUEUE
  return Object.entries(data).map(([nodeName, info]) => ({
    nodeName,
    status: info.status,
    currentUsers: info.current_users || [],
    bookingCount: (info.booking_list || []).length,
  }))
})

// --- JSON syntax highlighting ---
function highlightJson(text) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(
      /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
      (match) => {
        let cls = 'json-number'
        if (/^"/.test(match)) {
          cls = /:$/.test(match) ? 'json-key' : 'json-string'
        } else if (/true|false/.test(match)) {
          cls = 'json-boolean'
        } else if (/null/.test(match)) {
          cls = 'json-null'
        }
        return `<span class="${cls}">${match}</span>`
      }
    )
}

const highlightedJson = computed(() => highlightJson(stateText.value || ''))

const viewJsonHighlighted = computed(() => {
  if (!stateData.value) return ''
  return highlightJson(JSON.stringify(stateData.value, null, 2))
})

function syncScroll(e) {
  const pre = e.target.previousElementSibling
  if (pre) pre.scrollTop = e.target.scrollTop
}

onMounted(() => {
  refreshBot()
  fetchState()
  fetchCommandLogs()
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  clearInterval(stateTimer)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

async function refreshBot() {
  loading.value = true
  try {
    bot.value = await botsStore.getBot(route.params.id)
    // Init bot language from config_overrides
    try {
      const overrides = JSON.parse(bot.value.config_overrides || '{}')
      botLanguage.value = overrides.LANGUAGE || 'zh'
    } catch {
      botLanguage.value = 'zh'
    }
  } catch (e) {
    if (e.response?.status === 404) {
      router.replace({ name: 'NotFound' })
      return
    }
    ElMessage.error(t('common.error'))
  } finally {
    loading.value = false
  }
}

async function handleLanguageChange(lang) {
  try {
    await botsStore.setBotLanguage(bot.value.id, lang)
    ElMessage.success(t('common.success'))
  } catch {
    // revert on failure
    try {
      const overrides = JSON.parse(bot.value.config_overrides || '{}')
      botLanguage.value = overrides.LANGUAGE || 'zh'
    } catch {
      botLanguage.value = 'zh'
    }
  }
}

async function fetchState() {
  try {
    stateData.value = await botsStore.getBotState(route.params.id)
  } catch {
    stateData.value = null
  }
}

async function fetchCommandLogs() {
  try {
    const data = await botsStore.getBotLogs(route.params.id, {
      category: 'command',
      page: 1,
      limit: 1000,
    })
    commandLogs.value = data
  } catch {
    commandLogs.value = []
  }
}

const activeUsers = computed(() => {
  const oneDayAgo = Date.now() - 24 * 60 * 60 * 1000
  const userIds = new Set()
  for (const log of commandLogs.value) {
    if (log.created_at && new Date(log.created_at).getTime() < oneDayAgo) continue
    const match = log.message?.match(/^\[([^\]]+)\]/)
    if (match) userIds.add(match[1])
  }
  return [...userIds].sort()
})

const activeUserCount = computed(() => activeUsers.value.length)

const MAX_DISPLAY_USERS = 20
const displayedUsers = computed(() => activeUsers.value.slice(0, MAX_DISPLAY_USERS))
const overflowUserCount = computed(() => activeUsers.value.length - MAX_DISPLAY_USERS)

const totalCommands = computed(() => commandLogs.value.length)

const currentUtilization = computed(() => {
  if (!stateData.value || !isValidState(stateData.value)) return 0
  const data = stateData.value
  let total = 0,
    inUse = 0
  if (botType.value === 'DEVICE') {
    for (const devices of Object.values(data)) {
      if (Array.isArray(devices)) {
        for (const d of devices) {
          total++
          if (d.status !== 'idle') inUse++
        }
      }
    }
  } else {
    const entries = Object.values(data)
    total = entries.length
    inUse = entries.filter((n) => n && n.status !== 'idle').length
  }
  return total === 0 ? 0 : Math.round((inUse / total) * 100)
})

function startEditState() {
  stateText.value = stateData.value ? JSON.stringify(stateData.value, null, 2) : '{}'
  editingState.value = true
}

async function saveState() {
  let parsed
  try {
    parsed = JSON.parse(stateText.value)
  } catch {
    ElMessage.error(t('botCreate.invalidJson'))
    return
  }
  // Pre-validate on client side
  const cc =
    typeof bot.value.cluster_configs === 'string'
      ? JSON.parse(bot.value.cluster_configs)
      : bot.value.cluster_configs || {}
  const { warnings: clientWarnings } = validateBotState(parsed, bot.value.bot_type, cc, t)
  if (clientWarnings.length > 0) {
    try {
      await ElMessageBox.confirm(
        clientWarnings.join('\n'),
        t('clusterState.validationWarning', { count: clientWarnings.length }),
        {
          type: 'warning',
          confirmButtonText: t('common.confirm'),
          cancelButtonText: t('common.cancel'),
        }
      )
    } catch {
      return // user cancelled
    }
  }
  stateSaving.value = true
  try {
    const res = await botsStore.updateBotState(route.params.id, parsed)
    if (res.warnings && res.warnings.length > 0) {
      ElMessage.warning(res.warnings.join('\n'))
    } else {
      ElMessage.success(t('common.success'))
    }
    editingState.value = false
    fetchState()
  } catch {
    // handled by interceptor
  } finally {
    stateSaving.value = false
  }
}

function downloadState() {
  if (!stateData.value) {
    ElMessage.warning(t('clusterState.noState'))
    return
  }
  const blob = new Blob([JSON.stringify(stateData.value, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${bot.value.name || 'bot'}_state.json`
  a.click()
  URL.revokeObjectURL(url)
}

async function handleStart() {
  actionLoading.value = true
  try {
    await botsStore.startBot(bot.value.id)
    ElMessage.success(t('common.success'))
    await refreshBot()
  } catch {
    // handled by interceptor
  } finally {
    actionLoading.value = false
  }
}

async function handleStop() {
  actionLoading.value = true
  try {
    await botsStore.stopBot(bot.value.id)
    ElMessage.success(t('common.success'))
    await refreshBot()
  } catch {
    // handled by interceptor
  } finally {
    actionLoading.value = false
  }
}

async function handleTransferOwner() {
  const username = transferUsername.value.trim()
  if (!username) return
  try {
    await ElMessageBox.confirm(
      t('botDetail.transferOwnerConfirm', { username }),
      t('botDetail.transferOwner'),
      {
        confirmButtonText: t('common.confirm'),
        cancelButtonText: t('common.cancel'),
        type: 'warning',
      }
    )
  } catch {
    return
  }
  transferLoading.value = true
  try {
    await botsStore.transferOwner(bot.value.id, username)
    ElMessage.success(t('botDetail.transferOwnerSuccess', { username }))
    showTransferOwner.value = false
    transferUsername.value = ''
    await refreshBot()
  } catch {
    // handled by interceptor
  } finally {
    transferLoading.value = false
  }
}

async function handleDelete() {
  try {
    await ElMessageBox.confirm(t('botDetail.confirmDelete'), t('common.warning'), {
      type: 'warning',
    })
  } catch {
    return // user cancelled
  }
  actionLoading.value = true
  try {
    await botsStore.deleteBot(bot.value.id)
    ElMessage.success(t('common.success'))
    router.push('/bots')
  } catch {
    // handled by interceptor
  } finally {
    actionLoading.value = false
  }
}

function formatDate(d) {
  return formatDateTime(d)
}
</script>

<style scoped>
.group-tag {
  cursor: pointer;
  margin-right: 4px;
  margin-bottom: 2px;
}
.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  flex-wrap: wrap;
  gap: 12px;
}
.detail-header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}
.detail-title {
  margin: 0;
}
.detail-header-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.json-editor {
  position: relative;
  border: 1px solid var(--lb-border-light, #dcdfe6);
  border-radius: 4px;
}
.json-viewer {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.6;
  padding: 16px;
  margin: 0;
  background: var(--lb-bg-code, #f6f8fa);
  color: var(--lb-text-code, #303133);
  border-radius: 4px;
  border: 1px solid var(--lb-border-light, #dcdfe6);
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
.json-editor-backdrop,
.json-editor-textarea {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.6;
  padding: 16px;
  margin: 0;
  border: none;
  white-space: pre;
  word-wrap: normal;
  overflow: auto;
  tab-size: 2;
}
.json-editor-backdrop {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background: var(--lb-bg-code, #f6f8fa);
  color: var(--lb-text-code, #303133);
  overflow: auto;
  z-index: 0;
}
.json-editor-textarea {
  position: relative;
  z-index: 1;
  display: block;
  width: 100%;
  height: 320px;
  resize: vertical;
  background: transparent;
  color: transparent;
  caret-color: var(--lb-text-code, #303133);
  outline: none;
}
/* JSON syntax highlighting (unscoped for v-html) */
</style>
<style>
.json-key {
  color: #d73a49;
}
.json-string {
  color: #032f62;
}
.json-number {
  color: #005cc5;
}
.json-boolean {
  color: #005cc5;
}
.json-null {
  color: #6f42c1;
}
html.dark .json-key {
  color: #ff7b72;
}
html.dark .json-string {
  color: #a5d6ff;
}
html.dark .json-number {
  color: #79c0ff;
}
html.dark .json-boolean {
  color: #79c0ff;
}
html.dark .json-null {
  color: #d2a8ff;
}
</style>

<style scoped>
.secret-field code {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  flex: 1;
  min-width: 0;
}
.secret-icon {
  font-size: 15px;
  color: var(--el-text-color-secondary);
  cursor: pointer;
  flex-shrink: 0;
  margin-left: 4px;
}
.secret-icon:hover {
  color: var(--el-color-primary);
}
.callback-hint {
  line-height: 1.8;
}
.callback-hint-title {
  font-weight: 600;
  margin-bottom: 4px;
}
.detail-stat-box {
  text-align: center;
  padding: 12px;
  border-radius: 8px;
  background: var(--lb-bg-code);
}
.detail-stat-number {
  font-size: 28px;
  font-weight: 700;
  color: var(--lb-color-primary);
}
.detail-stat-desc {
  font-size: 13px;
  color: var(--lb-text-secondary);
  margin-top: 4px;
}
.active-users-section {
  margin-top: 16px;
  border-top: 1px solid var(--lb-border-light);
  padding-top: 12px;
}
.active-users-title {
  font-size: 13px;
  color: var(--lb-text-secondary);
  margin-bottom: 8px;
}
.active-users-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.cluster-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
}
.cluster-node-card {
  border: 1px solid var(--lb-border-light);
  border-radius: 10px;
  padding: 14px 16px;
  background: var(--el-bg-color);
  transition: box-shadow 0.2s;
}
.cluster-node-card:hover {
  box-shadow: var(--lb-shadow-card-hover, 0 2px 8px rgba(0, 0, 0, 0.06));
}
.cluster-node-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}
.cluster-node-name {
  font-weight: 600;
  font-size: 14px;
  color: var(--lb-text-primary);
  display: flex;
  align-items: center;
  gap: 6px;
}
.cluster-node-summary {
  font-size: 12px;
  color: var(--lb-text-secondary);
  background: var(--el-fill-color);
  padding: 2px 8px;
  border-radius: 10px;
}
.cluster-device-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.cluster-device-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 0;
}
.cluster-device-left {
  display: flex;
  align-items: center;
  gap: 8px;
}
.cluster-device-model {
  font-size: 13px;
  color: var(--lb-text-primary);
}
.cluster-device-users {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.status-dot--idle {
  background: var(--el-color-info);
}
.status-dot--exclusive {
  background: var(--el-color-danger);
}
.status-dot--shared {
  background: var(--el-color-warning);
}
.cluster-node-body {
  font-size: 13px;
}
.cluster-node-users {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.cluster-empty-text {
  color: var(--el-text-color-placeholder);
}
.cluster-node-booking {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-secondary);
}
</style>
