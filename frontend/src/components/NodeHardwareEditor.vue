<template>
  <section>
    <p v-if="errorKey" role="alert">{{ t(errorKey) }}</p>
    <template v-if="inventory">
      <div class="hardware-toolbar">
        <span>{{ t('nodeHardware.help') }}</span>
        <el-button size="small" @click="copyPrompt">{{ t('nodeAccess.copyPrompt') }}</el-button>
      </div>
      <JsonMappingEditor
        v-model="input"
        :label="t('nodeHardware.mapping')"
        :disabled="busy"
        :example="example"
        @update:model-value="resetPreview"
      />
      <p v-if="preview && !changed" role="status">{{ t('nodeAccess.noChanges') }}</p>
      <section v-if="changed" aria-live="polite">
        <p>{{ t('nodeHardware.review') }}</p>
        <ul>
          <li v-for="(accelerators, node) in preview" :key="node">
            <strong>{{ node }}</strong>
            {{ formatHardware(accelerators) || t('nodeHardware.removed') }}
          </li>
        </ul>
      </section>
      <details v-if="Object.keys(inventory.nodes).length" class="hardware-saved">
        <summary>{{ t('nodeHardware.saved') }}</summary>
        <ul>
          <li v-for="(accelerators, node) in inventory.nodes" :key="node">
            <strong>{{ node }}</strong> {{ formatHardware(accelerators) }}
          </li>
        </ul>
      </details>
    </template>
    <div class="actions">
      <el-button :disabled="busy" @click="emit('close')">{{ t('common.cancel') }}</el-button>
      <el-button
        v-if="!preview"
        type="primary"
        :loading="busy"
        :disabled="!input.trim() || !inventory"
        @click="validate"
        >{{ t('nodeAccess.validate') }}</el-button
      >
      <el-button v-else type="primary" :loading="busy" :disabled="!changed" @click="save">
        {{ t('nodeAccess.save') }}
      </el-button>
    </div>
  </section>
</template>

<script setup>
import { computed, defineAsyncComponent, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
import { useHelpers } from '../utils/helpers'

const JsonMappingEditor = defineAsyncComponent(() => import('./JsonMappingEditor.vue'))
const props = defineProps({ botId: { type: Number, required: true } })
const emit = defineEmits(['close'])
const { t } = useI18n()
const { copyText } = useHelpers()
const busy = ref(false)
const errorKey = ref('')
const input = ref('')
const inventory = ref(null)
const preview = ref(null)
const path = computed(() => `/bots/${props.botId}/node-hardware`)
const changed = computed(() => Object.keys(preview.value ?? {}).length > 0)
const requestOptions = { _silent: true }
const mappingOptions = {
  ...requestOptions,
  headers: { 'Content-Type': 'application/json' },
  transformRequest: [(body) => body],
}
const example = JSON.stringify(
  { version: 1, nodes: { 'node-01': [{ model: 'P900', count: 8 }] } },
  null,
  2
)

function formatHardware(accelerators) {
  return accelerators.map(({ model, count }) => `${model} × ${count}`).join(' · ')
}

function resetPreview() {
  preview.value = null
  errorKey.value = ''
}

async function run(action, fallback) {
  if (busy.value) return
  errorKey.value = ''
  busy.value = true
  try {
    await action()
  } catch (error) {
    errorKey.value =
      error.response?.data?.detail === 'node_hardware_unknown_node'
        ? 'nodeHardware.unknownNode'
        : fallback
  } finally {
    busy.value = false
  }
}

onMounted(() =>
  run(async () => {
    inventory.value = (await api.get(path.value, requestOptions)).data
  }, 'nodeHardware.readFailed')
)

function copyPrompt() {
  copyText(t('nodeHardware.prompt', { nodeIds: JSON.stringify(inventory.value.node_ids), example }))
}

function validate() {
  try {
    JSON.parse(input.value)
  } catch {
    errorKey.value = 'jsonEditor.invalidSyntax'
    return
  }
  return run(async () => {
    preview.value = (
      await api.post(`${path.value}/preview`, input.value, mappingOptions)
    ).data.changes
  }, 'nodeHardware.validateFailed')
}

function save() {
  return run(async () => {
    await api.patch(path.value, input.value, mappingOptions)
    emit('close')
    ElMessage.success(t('nodeHardware.saveSuccess'))
  }, 'nodeHardware.writeFailed')
}
</script>

<style scoped>
.actions {
  margin-top: 20px;
  text-align: right;
}
.hardware-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 12px;
}
.hardware-saved {
  margin-top: 12px;
}
</style>
