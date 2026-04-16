<template>
  <Teleport to="body">
    <div v-if="isDemoMode" class="demo-fab-wrapper">
      <!-- Chat panel -->
      <Transition name="demo-slide">
        <div v-show="isOpen" class="demo-panel">
          <!-- Header: title row -->
          <div class="demo-panel-header">
            <div class="demo-panel-header-left">
              <svg
                class="demo-panel-icon"
                viewBox="0 0 32 32"
                width="20"
                height="20"
                xmlns="http://www.w3.org/2000/svg"
              >
                <rect x="1" y="13" width="4" height="6" rx="1.5" fill="#409eff" opacity="0.7" />
                <rect x="27" y="13" width="4" height="6" rx="1.5" fill="#409eff" opacity="0.7" />
                <rect
                  x="5"
                  y="8"
                  width="22"
                  height="19"
                  rx="4"
                  fill="#e6f0ff"
                  stroke="#409eff"
                  stroke-width="1.8"
                />
                <circle cx="12" cy="16" r="2.5" fill="#409eff" />
                <circle cx="20" cy="16" r="2.5" fill="#409eff" />
                <circle cx="13" cy="15" r="0.8" fill="#fff" />
                <circle cx="21" cy="15" r="0.8" fill="#fff" />
                <rect x="11" y="21.5" width="10" height="2" rx="1" fill="#409eff" />
              </svg>
              <div>
                <div class="demo-panel-title">{{ $t('demoChat.title') }}</div>
                <div class="demo-panel-subtitle">{{ selectedBotName }}</div>
              </div>
            </div>
            <div class="demo-panel-header-actions">
              <el-button text size="small" :title="$t('demoChat.clear')" @click="clearMessages">
                <el-icon><Delete /></el-icon>
              </el-button>
              <el-button text size="small" aria-label="Close chat" @click="isOpen = false">
                <el-icon><Close /></el-icon>
              </el-button>
            </div>
          </div>

          <!-- Toolbar: bot & user selectors -->
          <div class="demo-panel-toolbar">
            <el-select
              v-model="selectedBotId"
              size="small"
              class="demo-select-bot"
              popper-class="demo-select-popper"
            >
              <el-option
                v-for="b in runningBots"
                :key="b.id"
                :label="botDisplayName(b)"
                :value="b.id"
              />
            </el-select>
            <el-select
              v-model="demoUserId"
              size="small"
              class="demo-select-user"
              popper-class="demo-select-popper"
            >
              <el-option
                v-for="u in demoUsers"
                :key="u.id"
                :label="u.username"
                :value="u.username"
              />
            </el-select>
          </div>

          <!-- Messages area -->
          <div ref="chatBoxRef" class="demo-panel-messages" role="log" aria-live="polite">
            <TransitionGroup name="demo-msg-anim">
              <div
                v-for="(msg, idx) in currentMessages"
                :key="msg._id || idx"
                class="demo-msg"
                :class="{
                  'demo-msg--bot': msg.role === 'bot',
                  'demo-msg--user': msg.role === 'user',
                }"
              >
                <!-- Bot avatar -->
                <div v-if="msg.role === 'bot'" class="demo-avatar demo-avatar--bot">
                  <svg
                    viewBox="0 0 32 32"
                    width="18"
                    height="18"
                    xmlns="http://www.w3.org/2000/svg"
                  >
                    <rect
                      x="5"
                      y="8"
                      width="22"
                      height="19"
                      rx="4"
                      fill="#e6f0ff"
                      stroke="#409eff"
                      stroke-width="1.5"
                    />
                    <circle cx="12" cy="16" r="2.2" fill="#409eff" />
                    <circle cx="20" cy="16" r="2.2" fill="#409eff" />
                    <rect x="11" y="21.5" width="10" height="1.8" rx="0.9" fill="#409eff" />
                  </svg>
                </div>
                <div class="demo-bubble">
                  <pre
                    v-if="msg.role === 'bot'"
                    class="demo-msg-text"
                    v-html="highlightMentions(msg.text)"
                  ></pre>
                  <span v-else class="demo-msg-text">{{ msg.text }}</span>
                </div>
                <!-- User avatar -->
                <div v-if="msg.role === 'user'" class="demo-avatar demo-avatar--user">
                  {{ (demoUserId || 'U')[0].toUpperCase() }}
                </div>
              </div>
            </TransitionGroup>

            <!-- Typing indicator -->
            <Transition name="demo-msg-anim">
              <div v-if="isTyping" class="demo-msg demo-msg--bot">
                <div class="demo-bubble demo-typing">
                  <span class="demo-typing-dot"></span>
                  <span class="demo-typing-dot"></span>
                  <span class="demo-typing-dot"></span>
                </div>
              </div>
            </Transition>

            <!-- Empty state -->
            <div v-if="!currentMessages.length && !isTyping" class="demo-placeholder">
              <div class="demo-placeholder-icon">
                <svg viewBox="0 0 32 32" width="40" height="40" xmlns="http://www.w3.org/2000/svg">
                  <rect x="1" y="13" width="4" height="6" rx="1.5" fill="#409eff" opacity="0.4" />
                  <rect x="27" y="13" width="4" height="6" rx="1.5" fill="#409eff" opacity="0.4" />
                  <rect
                    x="5"
                    y="8"
                    width="22"
                    height="19"
                    rx="4"
                    fill="#f0f5ff"
                    stroke="#409eff"
                    stroke-width="1.2"
                  />
                  <circle cx="12" cy="16" r="2.5" fill="#409eff" opacity="0.6" />
                  <circle cx="20" cy="16" r="2.5" fill="#409eff" opacity="0.6" />
                  <rect x="11" y="21.5" width="10" height="2" rx="1" fill="#409eff" opacity="0.6" />
                </svg>
              </div>
              <div class="demo-placeholder-text">{{ $t('demoChat.placeholder') }}</div>
              <div class="demo-quick-cmds">
                <button
                  v-for="cmd in quickCommands"
                  :key="cmd"
                  class="demo-quick-btn"
                  @click="runQuickCommand(cmd)"
                >
                  {{ cmd }}
                </button>
              </div>
            </div>
          </div>

          <!-- Command autocomplete -->
          <Transition name="demo-fade">
            <div v-if="filteredSuggestions.length > 0" class="demo-autocomplete">
              <div
                v-for="s in filteredSuggestions"
                :key="s.cmd"
                class="demo-autocomplete-item"
                @mousedown.prevent="applySuggestion(s)"
              >
                <span class="demo-autocomplete-cmd">{{ s.cmd }}</span>
                <span class="demo-autocomplete-tip">{{ s.tip }}</span>
              </div>
            </div>
          </Transition>

          <!-- Input -->
          <div class="demo-panel-input">
            <el-input
              ref="inputRef"
              v-model="inputText"
              :placeholder="$t('demoChat.inputPlaceholder')"
              size="default"
              @keyup.enter="sendMessage"
            >
              <template #append>
                <el-button :icon="Promotion" @click="sendMessage" />
              </template>
            </el-input>
          </div>
        </div>
      </Transition>

      <!-- FAB button -->
      <Transition name="demo-fade">
        <div
          v-show="!isOpen"
          class="demo-fab"
          role="button"
          tabindex="0"
          aria-label="Open demo chat"
          @click="openChat"
          @keydown.enter="openChat"
          @keydown.space.prevent="openChat"
        >
          <svg viewBox="0 0 32 32" width="28" height="28" xmlns="http://www.w3.org/2000/svg">
            <rect x="1" y="13" width="4" height="6" rx="1.5" fill="#fff" opacity="0.9" />
            <rect x="27" y="13" width="4" height="6" rx="1.5" fill="#fff" opacity="0.9" />
            <rect x="5" y="8" width="22" height="19" rx="4" fill="#fff" stroke="none" />
            <circle cx="12" cy="16" r="2.5" fill="#409eff" />
            <circle cx="20" cy="16" r="2.5" fill="#409eff" />
            <circle cx="13" cy="15" r="0.8" fill="#409eff" />
            <circle cx="21" cy="15" r="0.8" fill="#409eff" />
            <rect x="11" y="21.5" width="10" height="2" rx="1" fill="#409eff" />
          </svg>
          <!-- Pulse ring -->
          <div class="demo-fab-pulse"></div>
        </div>
      </Transition>

      <!-- Demo badge -->
      <div v-if="!isOpen" class="demo-badge">{{ $t('demoChat.demoBanner') }}</div>
    </div>
  </Teleport>
</template>

<script setup>
import { ref, computed, nextTick, watch } from 'vue'
import { Close, Delete, Promotion } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import { isDemoMode } from '../utils/demoMode'
import { executeCommand } from '../utils/demoBotEngine'
import { botStates, mockBots, mockUsers, appendLog } from '../utils/mockData'
import { getLocale } from '../i18n'

const { t } = useI18n()

/** Map vue-i18n locale to engine lang ('en' | 'zh'). */
function engineLang() {
  const loc = getLocale()
  return loc.startsWith('zh') ? 'zh' : 'en'
}

const isOpen = ref(false)
const selectedBotId = ref(null)
const demoUserId = ref('demo_user')
const inputText = ref('')
const inputRef = ref(null)
const messages = ref({})
const chatBoxRef = ref(null)
const isTyping = ref(false)
let msgIdCounter = 0

const demoUsers = computed(() => mockUsers.map((u) => ({ id: u.id, username: u.username })))

const quickCommands = computed(() => {
  const bot = selectedBot.value
  const cc = bot ? getClusterConfigs(bot.id) : {}
  const keys = Array.isArray(cc) ? cc : Object.keys(cc)
  const firstKey = keys[0] || 'node0'
  const type = bot?.bot_type || 'NODE'
  if (type === 'DEVICE') {
    return ['@bot', 'help', `lock ${firstKey} dev0`]
  }
  if (type === 'QUEUE') {
    return ['@bot', 'help', `book ${firstKey}`]
  }
  return ['@bot', 'help', `lock ${firstKey}`]
})

/** Translate bot name via i18n key if available. */
function botDisplayName(bot) {
  if (!bot) return ''
  return bot.name_i18n ? t(bot.name_i18n) : bot.name
}

const runningBots = computed(() => mockBots.filter((b) => b.status === 'running'))
const selectedBot = computed(() => mockBots.find((b) => b.id === selectedBotId.value))
const selectedBotName = computed(() => botDisplayName(selectedBot.value))
const currentMessages = computed(() => messages.value[selectedBotId.value] || [])

watch(
  runningBots,
  (bots) => {
    if (bots.length > 0 && !selectedBotId.value) {
      selectedBotId.value = bots[0].id
    }
  },
  { immediate: true }
)

function openChat() {
  isOpen.value = true
  if (runningBots.value.length > 0 && !selectedBotId.value) {
    selectedBotId.value = runningBots.value[0].id
  }
}

function clearMessages() {
  const botId = selectedBotId.value
  if (botId && messages.value[botId]) {
    messages.value[botId] = []
  }
}

function getClusterConfigs(botId) {
  const bot = mockBots.find((b) => b.id === botId)
  if (!bot) return {}
  try {
    return JSON.parse(bot.cluster_configs || '{}')
  } catch {
    return {}
  }
}

function getState(botId) {
  return botStates[botId] || null
}

function runQuickCommand(cmd) {
  if (cmd === '@bot') {
    sendQueryAsAt()
    return
  }
  inputText.value = cmd
  sendMessage()
}

function sendQueryAsAt() {
  const botId = selectedBotId.value
  if (!botId) return

  const bot = selectedBot.value
  if (!bot || bot.status !== 'running') return

  const state = getState(botId)
  if (!state) return

  const cc = getClusterConfigs(botId)
  const owner = mockUsers.find((u) => u.id === bot.user_id)
  const config = {
    CLUSTER_CONFIGS: cc,
    DEFAULT_DURATION: 7200,
    MAX_LOCK_DURATION: -1,
    EARLY_NOTIFY: false,
    TIME_ALERT: 300,
    BOT_ID: String(bot.id),
    BOT_OWNER: owner ? owner.username : '',
  }

  // Push "@Bot" as user message
  if (!messages.value[botId]) messages.value[botId] = []
  messages.value[botId].push({ _id: ++msgIdCounter, role: 'user', text: `@${bot.name}` })
  nextTick(scrollToBottom)

  isTyping.value = true
  nextTick(scrollToBottom)
  setTimeout(
    () => {
      isTyping.value = false
      const reply = executeCommand(state, demoUserId.value, '', bot.bot_type, config, engineLang())
      pushBotReply(botId, reply)
      appendLog(botId, 'command', 'INFO', `[${demoUserId.value}] @query`)
    },
    400 + Math.random() * 300
  )
}

/**
 * Highlight @mentions in bot message text.
 * Only highlights explicit @username patterns (notification/alert contexts).
 * @self → green, @others → blue. Escapes HTML first for safety.
 */
function highlightMentions(text) {
  // Escape HTML entities
  let escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')

  // Only match literal @username (not bare usernames in status output)
  const usernames = mockUsers.map((u) => u.username).sort((a, b) => b.length - a.length)
  for (const uname of usernames) {
    const isSelf = uname === demoUserId.value
    const cls = isSelf ? 'demo-mention-self' : 'demo-mention-other'
    const re = new RegExp(`@${escapeRegex(uname)}(?=\\b)`, 'g')
    escaped = escaped.replace(re, `<span class="${cls}">@${uname}</span>`)
  }
  return escaped
}

function escapeRegex(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// ---------------------------------------------------------------------------
// Command autocomplete
// ---------------------------------------------------------------------------

const commandHints = computed(() => {
  const bot = selectedBot.value
  const type = bot?.bot_type || 'NODE'
  const cc = bot ? getClusterConfigs(bot.id) : {}
  const keys = Array.isArray(cc) ? cc : Object.keys(cc)
  const ex = keys[0] || 'node'

  const base = [
    { cmd: `@${bot ? botDisplayName(bot) : 'bot'}`, tip: t('demoChat.cmdAtBot'), isAt: true },
    { cmd: 'help', tip: t('demoChat.cmdHelp') },
    { cmd: `lock ${ex}`, tip: t('demoChat.cmdLock') },
    { cmd: `lock ${ex} 2h`, tip: t('demoChat.cmdLockDuration') },
    { cmd: `unlock ${ex}`, tip: t('demoChat.cmdUnlock') },
    { cmd: 'free', tip: t('demoChat.cmdFree') },
    { cmd: `kickout ${ex}`, tip: t('demoChat.cmdKickout') },
    { cmd: `query ${ex}`, tip: t('demoChat.cmdQuery') },
  ]

  if (type === 'NODE' || type === 'DEVICE') {
    base.push({ cmd: `slock ${ex}`, tip: t('demoChat.cmdSlock') })
  }
  if (type === 'DEVICE') {
    const firstDevCount = Array.isArray(Object.values(cc)[0]) ? Object.values(cc)[0].length : 0
    const lastDev = Math.max(0, firstDevCount - 1)
    base.push({ cmd: `lock ${ex} dev0`, tip: t('demoChat.cmdLockDev') })
    if (lastDev > 0) {
      base.push({ cmd: `lock ${ex} dev0-${lastDev}`, tip: t('demoChat.cmdLockDevRange') })
    }
  }
  if (type === 'QUEUE') {
    base.push({ cmd: `book ${ex}`, tip: t('demoChat.cmdBook') })
    base.push({ cmd: `take ${ex}`, tip: t('demoChat.cmdTake') })
    base.push({ cmd: `kicklock ${ex}`, tip: t('demoChat.cmdKicklock') })
  }
  return base
})

const filteredSuggestions = computed(() => {
  const val = inputText.value.trim().toLowerCase()
  if (!val) return []
  return commandHints.value.filter((h) => h.cmd.toLowerCase().startsWith(val)).slice(0, 6)
})

function applySuggestion(suggestion) {
  if (suggestion.isAt) {
    inputText.value = ''
    sendQueryAsAt()
    return
  }
  inputText.value = suggestion.cmd
  nextTick(() => {
    inputRef.value?.focus?.()
  })
}

function sendMessage() {
  const cmd = inputText.value.trim()
  if (!cmd) return

  const botId = selectedBotId.value
  if (!botId) return

  // Per-bot messages
  if (!messages.value[botId]) messages.value[botId] = []
  messages.value[botId].push({ _id: ++msgIdCounter, role: 'user', text: cmd })
  inputText.value = ''
  nextTick(scrollToBottom)

  const bot = selectedBot.value
  if (!bot || bot.status !== 'running') {
    pushBotReply(botId, 'Bot is not running. Start it first.')
    return
  }

  const state = getState(botId)
  if (!state) {
    pushBotReply(botId, 'No cluster state available.')
    return
  }

  const cc = getClusterConfigs(botId)
  const owner = mockUsers.find((u) => u.id === bot.user_id)
  const config = {
    CLUSTER_CONFIGS: cc,
    DEFAULT_DURATION: 7200,
    MAX_LOCK_DURATION: -1,
    EARLY_NOTIFY: false,
    TIME_ALERT: 300,
    BOT_ID: String(bot.id),
    BOT_OWNER: owner ? owner.username : '',
  }

  // Show typing indicator, then reply
  isTyping.value = true
  nextTick(scrollToBottom)
  setTimeout(
    () => {
      isTyping.value = false
      const reply = executeCommand(state, demoUserId.value, cmd, bot.bot_type, config, engineLang())
      pushBotReply(botId, reply)
      appendLog(botId, 'command', 'INFO', `[${demoUserId.value}] ${cmd}`)
    },
    400 + Math.random() * 300
  )
}

function pushBotReply(botId, text) {
  if (!messages.value[botId]) messages.value[botId] = []
  messages.value[botId].push({ _id: ++msgIdCounter, role: 'bot', text })
  nextTick(scrollToBottom)
}

function scrollToBottom() {
  if (chatBoxRef.value) {
    chatBoxRef.value.scrollTop = chatBoxRef.value.scrollHeight
  }
}
</script>

<style scoped>
.demo-fab-wrapper {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 9999;
}

/* ── FAB ── */
.demo-fab {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--el-color-primary), var(--el-color-primary-light-3));
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  box-shadow: 0 4px 20px rgba(64, 158, 255, 0.4);
  transition:
    transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1),
    box-shadow 0.25s;
  position: relative;
  outline: none;
}
.demo-fab:hover,
.demo-fab:focus-visible {
  transform: scale(1.1);
  box-shadow: 0 6px 28px rgba(64, 158, 255, 0.5);
}
.demo-fab:focus-visible {
  outline: 2px solid var(--el-color-primary-light-5);
  outline-offset: 3px;
}
.demo-fab-pulse {
  position: absolute;
  inset: -4px;
  border-radius: 50%;
  border: 2px solid var(--el-color-primary);
  animation: demo-pulse 2s ease-out infinite;
  pointer-events: none;
}
@keyframes demo-pulse {
  0% {
    transform: scale(1);
    opacity: 0.6;
  }
  100% {
    transform: scale(1.5);
    opacity: 0;
  }
}

/* ── Badge ── */
.demo-badge {
  position: absolute;
  top: -8px;
  left: 50%;
  transform: translateX(-50%);
  background: linear-gradient(135deg, var(--el-color-success), var(--el-color-success-light-3));
  color: #fff;
  font-size: 10px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 10px;
  white-space: nowrap;
  pointer-events: none;
  letter-spacing: 0.3px;
}

/* ── Panel ── */
.demo-panel {
  position: absolute;
  bottom: 70px;
  right: 0;
  width: 480px;
  max-width: calc(100vw - 32px);
  height: 600px;
  max-height: calc(100vh - 100px);
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-light);
  border-radius: 16px;
  box-shadow:
    0 12px 48px rgba(0, 0, 0, 0.12),
    0 4px 16px rgba(0, 0, 0, 0.08);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── Header ── */
.demo-panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 16px;
  background: linear-gradient(135deg, var(--el-color-primary), var(--el-color-primary-dark-2));
  color: #fff;
  gap: 8px;
  flex-shrink: 0;
}
.demo-panel-header-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.demo-panel-icon {
  flex-shrink: 0;
}
.demo-panel-title {
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.2px;
}
.demo-panel-subtitle {
  font-size: 11px;
  opacity: 0.8;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.demo-panel-header-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
}
.demo-panel-header-actions :deep(.el-button) {
  color: rgba(255, 255, 255, 0.7);
}
.demo-panel-header-actions :deep(.el-button:hover) {
  color: #fff;
}

/* ── Toolbar ── */
.demo-panel-toolbar {
  display: flex;
  gap: 8px;
  padding: 8px 12px;
  background: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-lighter);
  flex-shrink: 0;
}
.demo-select-bot {
  flex: 1;
  min-width: 0;
}
.demo-select-user {
  width: 120px;
  flex-shrink: 0;
}

/* ── Messages ── */
.demo-panel-messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  background: var(--el-fill-color-lighter);
  scroll-behavior: smooth;
}
.demo-placeholder {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--el-text-color-placeholder);
  font-size: 13px;
}
.demo-placeholder-icon {
  opacity: 0.5;
  animation: demo-float 3s ease-in-out infinite;
}
@keyframes demo-float {
  0%,
  100% {
    transform: translateY(0);
  }
  50% {
    transform: translateY(-6px);
  }
}
.demo-placeholder-text {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

/* ── Quick commands ── */
.demo-quick-cmds {
  display: flex;
  gap: 8px;
  margin-top: 4px;
  flex-wrap: wrap;
  justify-content: center;
}
.demo-quick-btn {
  padding: 5px 14px;
  border: 1px solid var(--el-color-primary-light-7);
  border-radius: 16px;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
}
.demo-quick-btn:hover {
  background: var(--el-color-primary);
  color: #fff;
  border-color: var(--el-color-primary);
  transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.3);
}

/* ── Message bubbles ── */
.demo-msg {
  margin-bottom: 10px;
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.demo-msg--user {
  justify-content: flex-end;
}
.demo-msg--bot {
  justify-content: flex-start;
}

/* ── Avatars ── */
.demo-avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-top: 2px;
}
.demo-avatar--bot {
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-7);
}
.demo-avatar--user {
  background: linear-gradient(135deg, var(--el-color-primary), var(--el-color-primary-light-3));
  color: #fff;
  font-size: 12px;
  font-weight: 600;
}
.demo-bubble {
  max-width: 85%;
  padding: 8px 12px;
  border-radius: 12px;
  word-break: break-word;
}
.demo-msg--user .demo-bubble {
  background: linear-gradient(135deg, var(--el-color-primary), var(--el-color-primary-light-3));
  color: #fff;
  border-bottom-right-radius: 4px;
  box-shadow: 0 1px 4px rgba(64, 158, 255, 0.2);
}
.demo-msg--bot .demo-bubble {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-bottom-left-radius: 4px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}
.demo-msg-text {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}

/* ── @mention highlights ── */
:deep(.demo-mention-other) {
  color: #409eff;
  font-weight: 600;
  background: rgba(64, 158, 255, 0.08);
  border-radius: 3px;
  padding: 0 2px;
}
:deep(.demo-mention-self) {
  color: #67c23a;
  font-weight: 600;
  background: rgba(103, 194, 58, 0.08);
  border-radius: 3px;
  padding: 0 2px;
}

/* ── Typing indicator ── */
.demo-typing {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 10px 16px;
}
.demo-typing-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--el-color-primary-light-5);
  animation: demo-typing-bounce 1.4s ease-in-out infinite;
}
.demo-typing-dot:nth-child(2) {
  animation-delay: 0.16s;
}
.demo-typing-dot:nth-child(3) {
  animation-delay: 0.32s;
}
@keyframes demo-typing-bounce {
  0%,
  60%,
  100% {
    transform: translateY(0);
    opacity: 0.4;
  }
  30% {
    transform: translateY(-6px);
    opacity: 1;
  }
}

/* ── Input ── */
.demo-panel-input {
  padding: 10px 12px;
  border-top: 1px solid var(--el-border-color-lighter);
  flex-shrink: 0;
  background: var(--el-bg-color);
}

/* ── Autocomplete ── */
.demo-autocomplete {
  padding: 4px 8px;
  background: var(--el-bg-color);
  border-top: 1px solid var(--el-border-color-lighter);
  flex-shrink: 0;
  max-height: 180px;
  overflow-y: auto;
}
.demo-autocomplete-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s;
}
.demo-autocomplete-item:hover {
  background: var(--el-fill-color-light);
}
.demo-autocomplete-cmd {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-color-primary);
  white-space: nowrap;
}
.demo-autocomplete-tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── Message animation ── */
.demo-msg-anim-enter-active {
  transition: all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
}
.demo-msg-anim-leave-active {
  transition: all 0.2s ease;
}
.demo-msg-anim-enter-from {
  opacity: 0;
  transform: translateY(12px) scale(0.95);
}
.demo-msg-anim-leave-to {
  opacity: 0;
  transform: scale(0.95);
}

/* ── Panel transitions ── */
.demo-slide-enter-active,
.demo-slide-leave-active {
  transition: all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
}
.demo-slide-enter-from,
.demo-slide-leave-to {
  opacity: 0;
  transform: translateY(20px) scale(0.92);
}
.demo-fade-enter-active,
.demo-fade-leave-active {
  transition: opacity 0.2s;
}
.demo-fade-enter-from,
.demo-fade-leave-to {
  opacity: 0;
}
</style>

<style>
/* Global (non-scoped) — popper is teleported to body */
.demo-select-popper {
  z-index: 10000 !important;
}
</style>
