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
      <el-button size="small" :loading="refreshing" @click="refreshLogs">
        <el-icon><Refresh /></el-icon> {{ $t('log.refresh') }}
      </el-button>
      <el-button
        size="small"
        :disabled="logs.length === 0 || downloading"
        :loading="downloading"
        @click="downloadLogs"
      >
        <el-icon><Download /></el-icon> {{ $t('log.download') }}
      </el-button>
    </div>
    <p v-if="logs.some((entry) => entry.details_restricted)" class="log-privacy-note">
      {{ $t('log.privateDetailsRestricted') }}
    </p>
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
          <span v-if="logDisplay(log)" class="log-msg"
            ><el-tooltip :content="logDisplay(log).id"
              ><span>[{{ logDisplay(log).name }}]</span></el-tooltip
            >{{ logDisplay(log).rest }}</span
          >
          <span v-else class="log-msg">{{ log.message }}</span>
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
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { useBotsStore } from '../stores/bots'
import { useActorDisplay } from '../utils/useFeishuDisplayNames'
import { createLogViewerLoader } from '../utils/logViewerLoader'

const props = defineProps({
  botId: { type: [Number, String], required: true },
  includePrivate: { type: Boolean, default: false },
})

const botsStore = useBotsStore()
const actorNames = useActorDisplay()
function logDisplay(log) {
  const match =
    log.category === 'command' &&
    typeof log.message === 'string' &&
    /^\[(ou_[A-Za-z0-9_-]+)\]([\s\S]*)$/.exec(log.message)
  return match ? { id: match[1], name: actorNames.label(match[1]), rest: match[2] } : null
}
const logs = ref([])
const loading = ref(false)
const loadingMore = ref(false)
const level = ref('')
const category = ref('')
const autoRefresh = ref(false)
const hasMore = ref(false)
const refreshing = ref(false)
const downloading = ref(false)
let timer = null
const loader = createLogViewerLoader({
  request: (botId, params, signal) => botsStore.getBotLogs(botId, params, { signal }),
  publish: (update) => {
    if ('logs' in update) logs.value = update.logs
    if (update.page > 1) actorNames.refresh?.(update.page)
    if ('hasMore' in update) hasMore.value = update.hasMore
    if ('loading' in update) loading.value = update.loading
    if ('loadingMore' in update) loadingMore.value = update.loadingMore
    if ('refreshing' in update) refreshing.value = update.refreshing
    if ('downloading' in update) downloading.value = update.downloading
  },
  save: ({ rows, filename }) => {
    const text = rows
      .map(
        (log) => `[${formatTime(log.created_at)}] [${log.category}] ${log.level}: ${log.message}`
      )
      .join('\n')
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }))
    try {
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      link.click()
    } finally {
      URL.revokeObjectURL(url)
    }
  },
})
const scope = () => ({
  botId: props.botId,
  level: level.value,
  category: category.value,
  includePrivate: props.includePrivate,
})
const refreshLogs = () => loader.refresh()
const loadMore = () => loader.loadMore()
const downloadLogs = () => loader.download()

function formatTime(t) {
  if (!t) return ''
  // Treat naive datetime strings as UTC (old logs may lack 'Z')
  if (typeof t === 'string' && !t.endsWith('Z') && !t.includes('+')) {
    t = t + 'Z'
  }
  return new Date(t).toLocaleString()
}

watch(
  [() => props.botId, () => props.includePrivate, level, category],
  () => loader.setScope(scope()),
  { flush: 'sync' }
)
watch(autoRefresh, (val) => {
  clearInterval(timer)
  if (val) timer = setInterval(() => loader.refresh(), 5000)
})
onMounted(() => loader.setScope(scope()))
onBeforeUnmount(() => {
  loader.dispose()
  clearInterval(timer)
})
</script>

<style scoped>
.log-privacy-note {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin: 0 0 12px;
}
.log-header {
  display: flex;
  flex-wrap: wrap;
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
