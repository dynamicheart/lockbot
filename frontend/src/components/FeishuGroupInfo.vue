<template>
  <div class="feishu-group-info">
    <div class="group-name-row">
      <span v-if="group.name" class="group-name">{{ group.name }}</span>
      <span v-if="group.name && group.status !== 'ok'" class="group-placeholder">{{
        $t('feishu.groupCached')
      }}</span>
      <span v-else-if="!group.name && botId" class="group-placeholder">{{
        $t('feishu.groupUnknown')
      }}</span>
      <el-button
        text
        size="small"
        type="primary"
        :loading="loading"
        :disabled="!lookupPath"
        @click="load(true)"
      >
        {{
          group.status && group.status !== 'ok'
            ? $t('feishu.retryGroup')
            : group.name
              ? $t('common.refresh')
              : $t('feishu.lookupGroup')
        }}
      </el-button>
    </div>
    <el-button v-if="botId" text size="small" class="chat-id" @click="copyText(chatId)">
      <code>{{ chatId }}</code
      ><el-icon><CopyDocument /></el-icon>
    </el-button>
    <span v-if="group.status && group.status !== 'ok'" class="group-warning">
      {{ $t(warningKey) }}
    </span>
  </div>
</template>
<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { CopyDocument } from '@element-plus/icons-vue'
import api from '../utils/api'
import { useHelpers } from '../utils/helpers'
const props = defineProps({
  botId: { type: [Number, String], default: null },
  applicationId: { type: [Number, String], default: null },
  chatId: { type: String, default: '' },
})
const { copyText } = useHelpers()
const group = ref({})
const loading = ref(false)
const warningKey = computed(() => {
  if (!group.value.status || group.value.status === 'ok') return ''
  if (group.value.name)
    return group.value.status === 'permission'
      ? 'feishu.groupCachedPermission'
      : 'feishu.groupCachedUnavailable'
  return group.value.status === 'permission' ? 'feishu.groupPermission' : 'feishu.groupUnavailable'
})
let generation = 0
let controller = null
const lookupPath = computed(() => {
  if (props.botId) return `/feishu/bots/${props.botId}/group`
  return props.applicationId && /^oc_[A-Za-z0-9_]+$/.test(props.chatId)
    ? `/feishu/applications/${props.applicationId}/groups/${encodeURIComponent(props.chatId)}`
    : null
})
async function load(refresh = false) {
  if (!lookupPath.value || loading.value) return
  const run = generation
  const pending = new AbortController()
  controller = pending
  loading.value = true
  try {
    const { data } = await api.get(lookupPath.value, {
      params: { refresh },
      signal: pending.signal,
      _silent: true,
    })
    if (run === generation) group.value = data
  } catch {
    if (run === generation) group.value = { ...group.value, status: 'unavailable' }
  } finally {
    if (run === generation) {
      loading.value = false
      controller = null
    }
  }
}
watch(
  () => [props.botId, props.applicationId, props.chatId],
  () => {
    generation++
    controller?.abort()
    controller = null
    group.value = {}
    loading.value = false
    if (props.botId) load()
  },
  { immediate: true, flush: 'sync' }
)
onBeforeUnmount(() => {
  generation++
  controller?.abort()
  controller = null
})
</script>
<style scoped>
.feishu-group-info {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}
.group-name-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.group-name {
  font-weight: 500;
  overflow-wrap: anywhere;
}
.group-placeholder,
.chat-id {
  color: var(--el-text-color-secondary);
}
.chat-id {
  padding: 0;
  height: auto;
  max-width: 100%;
}
.chat-id code {
  white-space: normal;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.chat-id .el-icon {
  margin-left: 6px;
  flex-shrink: 0;
}
.group-warning {
  color: var(--el-color-warning-dark-2);
  font-size: 12px;
  line-height: 1.5;
}
</style>
