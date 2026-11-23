<template>
  <el-table v-if="inventory" :data="rows" size="small" max-height="260">
    <el-table-column :label="t('nodeInformation.node')" prop="node" min-width="85" />
    <el-table-column :label="t('nodeHardware.title')" min-width="190">
      <template #default="{ row }">
        <div v-for="(cards, index) in row.hardware" :key="index">
          {{ cards.model }} × {{ cards.count }}
          <span v-if="cards.memory_bytes_per_card">
            · {{ t('nodeInformation.perCard', { memory: gib(cards.memory_bytes_per_card) }) }}
          </span>
        </div>
        <div v-if="row.memory || row.cpu">
          {{ [row.memory && gib(row.memory), row.cpu].filter(Boolean).join(' · ') }}
        </div>
        <span v-if="!row.hardware?.length">{{ t('nodeInformation.notCollected') }}</span>
      </template>
    </el-table-column>
    <el-table-column :label="t('nodeAccess.loginAddress')" min-width="125">
      <template #default="{ row }">
        {{ row.access ? formatNodeAccess(row.access) : '—' }}
      </template>
    </el-table-column>
    <el-table-column :label="t('nodeInformation.collected')" min-width="144">
      <template #default="{ row }">
        <template v-if="row.observedAt">
          <div>{{ formatDateTime(row.observedAt) }}</div>
          <div>{{ t('nodeInformation.scope.' + row.scope) }}</div>
        </template>
        <span v-else>—</span>
      </template>
    </el-table-column>
  </el-table>
  <p v-else-if="failed" role="alert">{{ t('nodeAccess.readFailed') }}</p>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
import { useHelpers } from '../utils/helpers'
import { formatNodeAccess } from '../utils/nodeAccess'

const props = defineProps({ botId: { type: Number, required: true } })
const { t, locale } = useI18n()
const { formatDateTime } = useHelpers()
const inventory = ref(null)
const failed = ref(false)
const controller = new AbortController()
const rows = computed(() =>
  (inventory.value?.node_ids ?? []).map((node) => {
    const sample = inventory.value.nodes?.[node]
    return {
      node,
      hardware: sample?.accelerators ?? inventory.value.registered_hardware?.[node],
      memory: sample?.memory_bytes,
      cpu: sample?.cpu_arch,
      observedAt: sample?.observed_at,
      scope: sample?.scope,
      access: inventory.value.login_addresses?.[node],
    }
  })
)

function gib(bytes) {
  return (
    new Intl.NumberFormat(locale.value, { maximumFractionDigits: 1 }).format(bytes / 2 ** 30) +
    ' GiB'
  )
}

onMounted(async () => {
  try {
    const { data } = await api.get(`/bots/${props.botId}/node-inventory`, {
      _silent: true,
      signal: controller.signal,
    })
    inventory.value = data
  } catch {
    if (!controller.signal.aborted) failed.value = true
  }
})
onBeforeUnmount(() => controller.abort())
</script>
