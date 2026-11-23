<template>
  <el-button size="small" @click="open">{{ t('nodeInformation.open') }}</el-button>
  <el-dialog
    v-model="visible"
    destroy-on-close
    :title="t(editing ? 'nodeInformation.manualImport' : 'nodeInformation.title')"
    width="min(680px, calc(100vw - 32px))"
  >
    <NodeMetadataEditor
      v-if="editing"
      :bot-id="botId"
      :can-edit-hardware="canEditHardware"
      @changed="emit('changed')"
      @close="editing = false"
    />
    <NodeInventorySummary v-else-if="visible" :bot-id="botId" />
    <template v-if="!editing" #footer>
      <div class="node-information-actions">
        <router-link
          class="agent-registration-link"
          :to="{
            name: 'ApiCredentials',
            query: { bot: String(botId), scope: 'node_inventory:write,node_access:write' },
          }"
        >
          {{ t('nodeInformation.configureCollector') }}
        </router-link>
        <el-button type="primary" @click="editing = true">
          {{ t('nodeInformation.manualImport') }}
        </el-button>
      </div>
    </template>
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
const editing = ref(false)

function open() {
  editing.value = false
  visible.value = true
}
</script>

<style scoped>
.node-information-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}

.agent-registration-link {
  color: var(--el-text-color-regular);
  font-size: 13px;
  text-underline-offset: 2px;
}

.agent-registration-link:hover,
.agent-registration-link:focus-visible {
  color: var(--el-color-primary);
}
</style>
