<template>
  <section>
    <p v-if="errorKey" role="alert">{{ t(errorKey) }}</p>
    <template v-if="inventory">
      <details class="manual-import">
        <summary>{{ t('nodeInformation.manualImport') }}</summary>
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
      </details>
      <details v-if="Object.keys(inventory.nodes).length" class="hardware-saved">
        <summary>{{ t('nodeHardware.saved') }}</summary>
        <ul>
          <li v-for="(accelerators, node) in inventory.nodes" :key="node">
            <strong>{{ node }}</strong> {{ formatHardware(accelerators) }}
          </li>
        </ul>
      </details>
    </template>
  </section>
</template>

<script setup>
import { computed, defineAsyncComponent, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'

const JsonMappingEditor = defineAsyncComponent(() => import('./JsonMappingEditor.vue'))
const props = defineProps({ botId: { type: Number, required: true } })
const emit = defineEmits(['close'])
const { t } = useI18n()
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
.manual-import {
  margin-top: 12px;
}
.manual-import > summary {
  cursor: pointer;
  margin-bottom: 10px;
}
.actions {
  margin-top: 20px;
  text-align: right;
}
.hardware-saved {
  margin-top: 12px;
}
</style>
