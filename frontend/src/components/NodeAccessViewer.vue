<template>
  <el-popover
    v-model:visible="visible"
    :persistent="false"
    trigger="click"
    width="min(380px, calc(100vw - 32px))"
  >
    <template #reference>
      <el-button size="small">{{ t('nodeAccess.viewAddresses') }}</el-button>
    </template>
    <p v-if="error" class="addresses-message" role="alert">{{ t('nodeAccess.viewFailed') }}</p>
    <p v-else-if="addresses === null" class="addresses-message" role="status">
      {{ t('common.loading') }}
    </p>
    <p v-else-if="!addresses.length" class="addresses-message" role="status">
      {{ t('nodeAccess.emptyAddresses') }}
    </p>
    <dl v-else class="addresses">
      <div v-for="[node, access] in addresses" :key="node">
        <dt>{{ node }}</dt>
        <dd>
          <code>{{ formatNodeAccess(access) }}</code>
        </dd>
      </div>
    </dl>
  </el-popover>
</template>

<script setup>
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
import { formatNodeAccess } from '../utils/nodeAccess'

const props = defineProps({ botId: { type: Number, required: true } })
const { t } = useI18n()
const visible = ref(false)
const addresses = ref(null)
const error = ref(false)

watch([visible, () => props.botId], async ([open, botId], previous, onCleanup) => {
  addresses.value = null
  error.value = false
  if (!open) return
  if (previous[1] !== botId) {
    visible.value = false
    return
  }
  const request = new AbortController()
  onCleanup(() => request.abort())
  try {
    const { data } = await api.get(`/bots/${botId}/node-access`, {
      signal: request.signal,
      _silent: true,
    })
    if (!request.signal.aborted) {
      addresses.value = Object.entries(data.nodes)
    }
  } catch {
    if (!request.signal.aborted) error.value = true
  }
})
</script>

<style scoped>
.addresses-message {
  margin: 0;
}
.addresses {
  display: grid;
  gap: 8px;
  margin: 0;
  max-height: min(50vh, 320px);
  overflow: auto;
  overflow-wrap: anywhere;
}
.addresses dt {
  font-weight: 600;
}
.addresses dd {
  margin: 2px 0 0;
}
</style>
