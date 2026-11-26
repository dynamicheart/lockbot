<template>
  <el-popover trigger="click" width="min(340px, calc(100vw - 32px))">
    <template #reference>
      <el-button class="node-address-trigger" link size="small" type="primary">
        {{ $t('nodeAccess.loginAddress') }}
      </el-button>
    </template>
    <dl class="node-address">
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
  </el-popover>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({ address: { type: Object, required: true } })
const hostIsIp = computed(() => {
  const host = props.address.host
  return host.includes(':') || /^\d+(?:\.\d+){3}$/.test(host)
})
</script>

<style scoped>
.node-address-trigger.is-link {
  border-inline-width: 0;
  padding-inline: 0;
}

.node-address {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 6px 16px;
  margin: 0;
}

.node-address dt {
  color: var(--el-text-color-secondary);
}

.node-address dd {
  margin: 0;
  overflow-wrap: anywhere;
}
</style>
