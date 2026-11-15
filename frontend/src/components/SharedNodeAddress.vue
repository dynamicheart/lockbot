<template>
  <el-popover
    v-model:visible="visible"
    :persistent="false"
    trigger="click"
    width="min(340px, calc(100vw - 32px))"
  >
    <template #reference>
      <el-button link size="small" type="primary">{{ $t('nodeAccess.loginAddress') }}</el-button>
    </template>
    <code v-if="address && address.deadline > clock" class="shared-node-address">{{
      formatNodeAccess(address)
    }}</code>
    <span v-else role="status">{{
      $t(loading ? 'common.loading' : 'nodeAccess.sharedUnavailable')
    }}</span>
  </el-popover>
</template>

<script setup>
import { ref, watch } from 'vue'
import { formatNodeAccess } from '../utils/nodeAccess'

const props = defineProps({
  node: { type: String, required: true },
  clock: { type: Number, required: true },
  revision: { type: Number, required: true },
  load: { type: Function, required: true },
})
const visible = ref(false)
const address = ref(null)
const loading = ref(false)

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
.shared-node-address {
  overflow-wrap: anywhere;
}
</style>
