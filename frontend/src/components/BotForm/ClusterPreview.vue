<template>
  <div v-if="queryText" class="cluster-preview">
    <div class="preview-header">
      <span class="preview-title">{{ $t('botCreate.clusterPreviewTitle') }}</span>
      <el-tag size="small" effect="plain" type="info" class="preview-badge">
        {{ $t('botCreate.clusterPreviewBadge') }}
      </el-tag>
    </div>
    <pre class="preview-text">{{ queryText }}</pre>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { executeCommand } from '../../utils/demoBotEngine.js'

const props = defineProps({
  botType: { type: String, required: true },
  clusterConfigs: { type: [Array, Object], default: null },
})

const { locale } = useI18n()
const lang = computed(() => (locale.value === 'zh-CN' ? 'zh' : 'en'))

// Fixed mock start time so durations are stable (not recomputing every second)
const NOW = Math.floor(Date.now() / 1000)
const T2H = NOW - 2 * 3600 // started 2h ago
const T1H = NOW - 1 * 3600 // started 1h ago

/** Build a mock bot state from the user's current cluster_configs. */
const mockState = computed(() => {
  const cc = props.clusterConfigs
  if (!cc) return null

  if (props.botType === 'DEVICE') {
    if (typeof cc !== 'object' || Array.isArray(cc)) return null
    const entries = Object.entries(cc).filter(
      ([, models]) => Array.isArray(models) && models.length
    )
    if (!entries.length) return null

    // Walk devices globally: #0 exclusive alice, #1 shared bob+carol, rest idle
    let gIdx = 0
    const state = {}
    for (const [nodeName, models] of entries) {
      state[nodeName] = models.map((model, devIdx) => {
        const i = gIdx++
        if (i === 0)
          return {
            dev_id: devIdx,
            dev_model: model,
            status: 'exclusive',
            current_users: [{ user_id: 'alice', start_time: T2H, duration: 4 * 3600 }],
          }
        if (i === 1)
          return {
            dev_id: devIdx,
            dev_model: model,
            status: 'shared',
            current_users: [
              { user_id: 'bob', start_time: T1H, duration: 3 * 3600 },
              { user_id: 'carol', start_time: T1H, duration: 3 * 3600 },
            ],
          }
        return { dev_id: devIdx, dev_model: model, status: 'idle', current_users: [] }
      })
    }
    return state
  }

  // NODE / QUEUE
  if (!Array.isArray(cc)) return null
  const nodes = cc.filter(Boolean)
  if (!nodes.length) return null

  const isQueue = props.botType === 'QUEUE'
  const state = {}
  nodes.forEach((name, idx) => {
    if (idx === 0) {
      state[name] = {
        status: 'exclusive',
        current_users: [{ user_id: 'alice', start_time: T2H, duration: 4 * 3600 }],
        booking_list: isQueue
          ? [
              { user_id: 'bob', start_time: T1H, duration: 2 * 3600 },
              { user_id: 'carol', start_time: T1H, duration: 3 * 3600 },
            ]
          : [],
      }
    } else if (idx === 1 && !isQueue) {
      state[name] = {
        status: 'shared',
        current_users: [
          { user_id: 'bob', start_time: T1H, duration: 3 * 3600 },
          { user_id: 'carol', start_time: T1H, duration: 3 * 3600 },
        ],
        booking_list: [],
      }
    } else {
      state[name] = { status: 'idle', current_users: [], booking_list: [] }
    }
  })
  return state
})

const queryText = computed(() => {
  const state = mockState.value
  if (!state) return ''
  const cc = props.clusterConfigs
  return executeCommand(state, 'alice', '', props.botType, { CLUSTER_CONFIGS: cc }, lang.value)
})
</script>

<style scoped>
.cluster-preview {
  margin-top: 16px;
}

.preview-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.preview-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--lb-text-secondary, #6b7280);
}

.preview-badge {
  font-size: 11px;
  opacity: 0.75;
}

.preview-text {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--lb-text-primary, #111827);
  background: var(--el-fill-color-light);
  border: 1px solid var(--lb-border-light, #e5e7eb);
  border-radius: 8px;
  padding: 12px 14px;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  /* Prevent overflow when user adds many nodes */
  max-height: 260px;
  overflow-y: auto;
}
</style>
