<template>
  <el-popover
    v-model:visible="visible"
    :persistent="false"
    trigger="click"
    width="min(340px, calc(100vw - 32px))"
  >
    <template #reference>
      <el-button class="shared-node-address-trigger" link size="small" type="primary">
        {{ $t('nodeAccess.loginAddress') }}
      </el-button>
    </template>
    <dl v-if="address && address.deadline > clock" class="shared-node-address">
      <dt>{{ $t(hostIsIp ? 'nodeAccess.ipAddress' : 'nodeAccess.hostname') }}</dt>
      <dd>
        <code>{{ address.host }}</code>
      </dd>
      <template v-if="address.ip">
        <dt>{{ $t('nodeAccess.ipAddress') }}</dt>
        <dd>
          <code>{{ address.ip }}</code>
        </dd>
      </template>
      <dt>{{ $t('nodeAccess.sshPort') }}</dt>
      <dd>
        <code>{{ address.port }}</code>
      </dd>
    </dl>
    <span v-else role="status">{{
      $t(loading ? 'common.loading' : 'nodeAccess.sharedUnavailable')
    }}</span>
  </el-popover>
</template>

<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  node: { type: String, required: true },
  clock: { type: Number, required: true },
  revision: { type: Number, required: true },
  load: { type: Function, required: true },
})
const visible = ref(false)
const address = ref(null)
const loading = ref(false)
const hostIsIp = computed(() => {
  const host = address.value?.host ?? ''
  return host.includes(':') || /^\d+(?:\.\d+){3}$/.test(host)
})

watch([visible, () => props.node, () => props.revision], async ([open, node], _, onCleanup) => {
  address.value = null
  loading.value = open
  if (!open) return
  const request = new AbortController()
  onCleanup(() => request.abort())
  try {
    const result = await props.load(node, request.signal)
    if (!request.signal.aborted) address.value = result
  } catch {
    // A failed or revoked lookup clears the address before it can reappear.
  } finally {
    if (!request.signal.aborted) loading.value = false
  }
})
</script>

<style scoped>
.shared-node-address-trigger.is-link {
  border-inline-width: 0;
  padding-inline: 0;
}

.shared-node-address {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 6px 16px;
  margin: 0;
}

.shared-node-address dt {
  color: var(--el-text-color-secondary);
}

.shared-node-address dd {
  margin: 0;
  overflow-wrap: anywhere;
}
</style>
