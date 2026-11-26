<template>
  <div v-if="inventory" class="node-inventory-actions">
    <el-button v-if="showRawJson" size="small" @click="copyText(rawJson)">
      {{ t('nodeInformation.copyJson') }}
    </el-button>
    <el-button size="small" @click="showRawJson = !showRawJson">
      {{ t(showRawJson ? 'nodeInformation.backToList' : 'nodeInformation.rawJson') }}
    </el-button>
  </div>
  <pre
    v-if="inventory && showRawJson"
    class="node-inventory-json"
    :aria-label="t('nodeInformation.rawJson')"
    tabindex="0"
    >{{ rawJson }}</pre
  >
  <el-table v-else-if="inventory" :data="rows" row-key="node" size="small" max-height="320">
    <el-table-column :label="t('nodeInformation.details')" type="expand" width="60">
      <template #default="{ row }">
        <section class="node-inventory-details">
          <dl>
            <dt>{{ t('nodeAccess.hostname') }}</dt>
            <dd>{{ row.hostname || '—' }}</dd>
            <dt>{{ t('nodeAccess.ipAddress') }}</dt>
            <dd>{{ row.ip || '—' }}</dd>
            <dt>{{ t('nodeAccess.sshPort') }}</dt>
            <dd>{{ row.access?.port ?? '—' }}</dd>
            <template v-if="row.access?.method">
              <dt>{{ t('nodeInformation.loginMethod') }}</dt>
              <dd>{{ row.access.method }}</dd>
            </template>
            <template v-if="row.access?.note">
              <dt>{{ t('nodeInformation.loginNote') }}</dt>
              <dd>{{ row.access.note }}</dd>
            </template>
          </dl>
          <dl>
            <dt>{{ t('nodeHardware.title') }}</dt>
            <dd>
              <div v-for="(cards, index) in row.hardware" :key="index">
                {{ cards.model }} × {{ cards.count }}
                <span v-if="cards.memory_bytes_per_card">
                  · {{ t('nodeInformation.perCard', { memory: gib(cards.memory_bytes_per_card) }) }}
                </span>
              </div>
              <span v-if="!row.hardware?.length">—</span>
            </dd>
            <dt>{{ t('nodeInformation.totalMemory') }}</dt>
            <dd>{{ row.memory ? gib(row.memory) : '—' }}</dd>
            <dt>{{ t('nodeInformation.cpuArchitecture') }}</dt>
            <dd>{{ row.cpu || '—' }}</dd>
            <dt>{{ t('nodeInformation.collected') }}</dt>
            <dd>
              {{ row.observedAt ? formatDateTime(row.observedAt) : '—' }}
              <span v-if="row.scope"> · {{ t('nodeInformation.scope.' + row.scope) }}</span>
            </dd>
          </dl>
          <div v-if="row.attributes?.length || row.devices?.length" class="node-inventory-extra">
            <h3>{{ t('nodeInformation.extraAttributes') }}</h3>
            <dl v-if="row.attributes?.length">
              <template v-for="(attribute, index) in row.attributes" :key="index">
                <dt>{{ attribute.label }}</dt>
                <dd>{{ [attribute.value, attribute.unit].filter(Boolean).join(' ') }}</dd>
              </template>
            </dl>
            <div v-for="device in row.devices" :key="device.index" class="node-inventory-device">
              <h4>{{ t('nodeInformation.deviceIndex', { index: device.index }) }}</h4>
              <dl>
                <template v-for="(attribute, index) in device.attributes" :key="index">
                  <dt>{{ attribute.label }}</dt>
                  <dd>{{ [attribute.value, attribute.unit].filter(Boolean).join(' ') }}</dd>
                </template>
              </dl>
            </div>
          </div>
        </section>
      </template>
    </el-table-column>
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
        <span v-if="!row.hardware?.length">—</span>
      </template>
    </el-table-column>
    <el-table-column :label="t('nodeAccess.loginAddress')" min-width="125">
      <template #default="{ row }">
        <div class="node-inventory-address">
          {{ row.access ? formatNodeAccess(row.access) : '—' }}
        </div>
        <div v-if="row.access?.method">{{ row.access.method }}</div>
        <div v-if="row.access?.note">{{ row.access.note }}</div>
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
const { formatDateTime, copyText } = useHelpers()
const inventory = ref(null)
const showRawJson = ref(false)
const rawJson = computed(() => JSON.stringify(inventory.value, null, 2))
const failed = ref(false)
const controller = new AbortController()
const rows = computed(() =>
  (inventory.value?.node_ids ?? []).map((node) => {
    const sample = inventory.value.nodes?.[node]
    const access = inventory.value.login_addresses?.[node]
    const host = access?.host ?? ''
    const hostIsIp = host.includes(':') || /^\d+(?:\.\d+){3}$/.test(host)
    return {
      node,
      hardware: sample?.accelerators ?? inventory.value.registered_hardware?.[node],
      memory: sample?.memory_bytes,
      cpu: sample?.cpu_arch,
      observedAt: sample?.observed_at,
      scope: sample?.scope,
      attributes: sample?.attributes,
      devices: sample?.devices,
      access,
      hostname: hostIsIp ? '' : host,
      ip: access?.ip ?? (hostIsIp ? host : ''),
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

<style scoped>
.node-inventory-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-bottom: 8px;
}

.node-inventory-json {
  box-sizing: border-box;
  max-height: 320px;
  margin: 0;
  padding: 12px 16px;
  overflow: auto;
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base);
  background: var(--el-fill-color-lighter);
  color: var(--el-text-color-primary);
  font:
    13px/1.5 ui-monospace,
    monospace;
}

.node-inventory-address {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.node-inventory-details {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 24px;
  padding: 12px 16px;
  background: var(--el-fill-color-lighter);
}

.node-inventory-details dl {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  align-content: start;
  gap: 4px 12px;
  margin: 0;
}

.node-inventory-details dt {
  color: var(--el-text-color-secondary);
}

.node-inventory-details dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.node-inventory-extra {
  grid-column: 1 / -1;
  padding-top: 12px;
  border-top: 1px solid var(--el-border-color);
}

.node-inventory-extra h3,
.node-inventory-extra h4 {
  margin: 0 0 6px;
  font-size: inherit;
}

.node-inventory-device {
  margin-top: 10px;
}

@media (max-width: 600px) {
  .node-inventory-details {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
