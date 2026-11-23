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
      <el-button @click="editing = true">{{ t('nodeInformation.manualImport') }}</el-button>
      <router-link
        v-slot="{ href, navigate }"
        custom
        :to="{
          name: 'ApiCredentials',
          query: { bot: String(botId), scope: 'node_inventory:write,node_access:write' },
        }"
      >
        <el-button tag="a" type="primary" :href="href" @click="navigate">
          {{ t('nodeInformation.configureCollector') }}
        </el-button>
      </router-link>
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
