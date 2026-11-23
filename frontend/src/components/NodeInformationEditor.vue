<template>
  <el-button size="small" @click="open">{{ t('nodeInformation.open') }}</el-button>
  <el-dialog
    v-model="visible"
    destroy-on-close
    :title="t('nodeInformation.title')"
    width="min(680px, calc(100vw - 32px))"
  >
    <router-link
      class="collector-link"
      :to="{ name: 'ApiCredentials', query: { bot: String(botId), scope: 'node_inventory:write' } }"
    >
      {{ t('nodeInformation.configureCollector') }}
    </router-link>
    <NodeInventorySummary v-if="visible" :bot-id="botId" />
    <NodeMetadataEditor
      :bot-id="botId"
      :can-edit-hardware="canEditHardware"
      @changed="emit('changed')"
      @close="visible = false"
    />
  </el-dialog>
</template>

<script setup>
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import NodeInventorySummary from './NodeInventorySummary.vue'
import NodeMetadataEditor from './NodeMetadataEditor.vue'

defineProps({ botId: { type: Number, required: true }, canEditHardware: Boolean })
const emit = defineEmits(['changed'])
const { t } = useI18n()
const visible = ref(false)

function open() {
  visible.value = true
}
</script>

<style scoped>
.collector-link {
  display: inline-block;
  margin-bottom: 10px;
}
</style>
