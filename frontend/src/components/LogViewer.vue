<template>
  <div>
    <div class="log-header">
      <el-radio-group v-model="category" size="small">
        <el-radio-button value="">{{ $t('log.all') }}</el-radio-button>
        <el-radio-button value="command">{{ $t('log.commandLog') }}</el-radio-button>
        <el-radio-button value="system">{{ $t('log.systemLog') }}</el-radio-button>
      </el-radio-group>
      <el-select
        v-model="level"
        :placeholder="$t('log.allLevels')"
        clearable
        size="small"
        style="width: 120px"
      >
        <el-option label="INFO" value="INFO" />
        <el-option label="WARNING" value="WARNING" />
        <el-option label="ERROR" value="ERROR" />
      </el-select>
      <!-- <el-switch v-model="autoRefresh" :active-text="$t('log.autoRefresh')" size="small" /> -->
      <el-button size="small" @click="fetchLogs">
        <el-icon><Refresh /></el-icon> {{ $t('log.refresh') }}
      </el-button>
      <el-button size="small" :disabled="logs.length === 0" @click="downloadLogs">
        <el-icon><Download /></el-icon> {{ $t('log.download') }}
      </el-button>
    </div>
    <div ref="logContainer" class="log-container">
      <div v-if="loading" style="text-align: center; padding: 20px">
        <el-icon class="is-loading"><Loading /></el-icon> {{ $t('common.loading') }}
      </div>
      <div
        v-else-if="logs.length === 0"
        style="text-align: center; padding: 20px; color: var(--lb-text-secondary)"
      >
        {{ $t('log.noLogs') }}
      </div>
      <div v-else>
        <div
          v-for="log in logs"
          :key="log.id"
          class="log-line"
          :class="'log-' + log.level.toLowerCase()"
        >
          <span class="log-time">{{ formatTime(log.created_at) }}</span>
          <span :class="'log-level log-level-' + log.level.toLowerCase()">{{ log.level }}</span>
          <span v-if="log.category === 'command'" class="log-cmd">CMD</span>
          <span class="log-msg">{{ log.message }}</span>
        </div>
      </div>
    </div>
    <div v-if="hasMore" style="text-align: center; padding: 8px">
      <el-button
        size="small"
        text
        :loading="loadingMore"
        :disabled="loadingMore"
        @click="loadMore"
        >{{ $t('log.loadMore') }}</el-button
      >
    </div>
  </div>
</template>

<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue'
import { useBotsStore } from '../stores/bots'

const props = defineProps({
  botId: { type: [Number, String], required: true },
})

const botsStore = useBotsStore()
const logs = ref([])
const loading = ref(false)
const loadingMore = ref(false)
const level = ref('')
const category = ref('')
const autoRefresh = ref(false)
const page = ref(1)
const hasMore = ref(false)
let timer = null

async function fetchLogs(append = false) {
  if (append) {
    loadingMore.value = true
  } else {
    loading.value = true
  }
  try {
    const params = { page: page.value, limit: 50 }
    if (level.value) params.level = level.value
    if (category.value) params.category = category.value
    const data = await botsStore.getBotLogs(props.botId, params)
    if (append) {
      logs.value = [...logs.value, ...data]
    } else {
      logs.value = data
    }
    hasMore.value = data.length === 50
  } catch {
    // handled by interceptor
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

function loadMore() {
  if (loadingMore.value) return
  page.value++
  fetchLogs(true)
}

function formatTime(t) {
  if (!t) return ''
  // Treat naive datetime strings as UTC (old logs may lack 'Z')
  if (typeof t === 'string' && !t.endsWith('Z') && !t.includes('+')) {
    t = t + 'Z'
  }
  return new Date(t).toLocaleString()
}

async function downloadLogs() {
  // Fetch all logs (no pagination) for current filters
  try {
    const params = { page: 1, limit: 10000 }
    if (level.value) params.level = level.value
    if (category.value) params.category = category.value
    const allLogs = await botsStore.getBotLogs(props.botId, params)

    const text = allLogs
      .map(
        (log) => `[${formatTime(log.created_at)}] [${log.category}] ${log.level}: ${log.message}`
      )
      .join('\n')

    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const suffix = category.value || 'all'
    a.download = `bot_${props.botId}_${suffix}_logs.txt`
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    // handled by interceptor
  }
}

watch(level, () => {
  page.value = 1
  fetchLogs()
})

watch(category, () => {
  page.value = 1
  fetchLogs()
})

watch(autoRefresh, (val) => {
  if (val) {
    timer = setInterval(() => fetchLogs(), 5000)
  } else {
    clearInterval(timer)
  }
})

onMounted(() => {
  fetchLogs()
})
onUnmounted(() => {
  clearInterval(timer)
})
</script>

<style scoped>
.log-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.log-container {
  max-height: 400px;
  overflow-y: auto;
  background: var(--lb-bg-terminal);
  border: 1px solid var(--lb-terminal-border);
  border-radius: 6px;
  padding: 12px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 13px;
}
.log-line {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 2px 0;
  color: var(--lb-terminal-text);
}
.log-time {
  color: var(--lb-terminal-time);
  white-space: nowrap;
  flex-shrink: 0;
}
.log-tag {
  flex-shrink: 0;
}
.log-level {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 3px;
  flex-shrink: 0;
  letter-spacing: 0.3px;
}
.log-level-info {
  color: var(--lb-text-secondary);
  background: var(--lb-border-color);
}
.log-level-warning {
  color: var(--lb-log-warning-text);
  background: var(--lb-log-warning-bg);
}
.log-level-error {
  color: #fff;
  background: var(--lb-color-danger);
}
.log-cmd {
  font-size: 11px;
  font-weight: 600;
  color: var(--lb-log-cmd-text);
  background: var(--lb-log-cmd-bg);
  padding: 1px 6px;
  border-radius: 3px;
  flex-shrink: 0;
}
.log-msg {
  word-break: break-all;
  white-space: pre-wrap;
}
.log-error .log-msg {
  color: var(--lb-color-danger);
  font-weight: 500;
}
.log-warning .log-msg {
  color: var(--lb-color-warning);
}
</style>
