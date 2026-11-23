<template>
  <el-button size="small" @click="open">{{ t('nodeInformation.open') }}</el-button>
  <el-dialog
    v-model="visible"
    destroy-on-close
    :title="t('nodeInformation.title')"
    width="min(620px, calc(100vw - 32px))"
  >
    <el-tabs v-if="canEditHardware" v-model="section">
      <el-tab-pane name="access" :label="t('nodeAccess.loginAddress')" lazy>
        <NodeAccessEditor :bot-id="botId" @changed="emit('changed')" @close="visible = false" />
      </el-tab-pane>
      <el-tab-pane name="hardware" :label="t('nodeHardware.title')" lazy>
        <NodeHardwareEditor :bot-id="botId" @close="visible = false" />
      </el-tab-pane>
    </el-tabs>
    <NodeAccessEditor v-else :bot-id="botId" @changed="emit('changed')" @close="visible = false" />
  </el-dialog>
</template>

<script setup>
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import NodeAccessEditor from './NodeAccessEditor.vue'
import NodeHardwareEditor from './NodeHardwareEditor.vue'

defineProps({ botId: { type: Number, required: true }, canEditHardware: Boolean })
const emit = defineEmits(['changed'])
const { t } = useI18n()
const visible = ref(false)
const section = ref('access')

function open() {
  section.value = 'access'
  visible.value = true
}
</script>
