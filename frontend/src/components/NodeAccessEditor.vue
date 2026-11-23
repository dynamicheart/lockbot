<template>
  <section>
    <p>{{ t('nodeAccess.purpose') }}</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <template v-if="inventory">
      <div class="mapping-header">
        <span>{{ t('nodeAccess.help') }}</span>
        <el-button size="small" @click="copyPrompt">{{ t('nodeAccess.copyPrompt') }}</el-button>
      </div>
      <div class="mapping">
        <span>{{ t('nodeAccess.mapping') }}</span>
        <JsonMappingEditor
          v-model="input"
          :label="t('nodeAccess.mapping')"
          :disabled="busy"
          :example="example"
          @update:model-value="onEdit"
        />
      </div>
      <div v-if="preview" role="status" class="preview">
        <p>{{ t('nodeAccess.review') }}</p>
        <ul>
          <li v-for="node in [...preview.added, ...preview.changed]" :key="node">
            <strong>{{ node }}</strong>
            <template v-if="inventory.nodes[node]">
              {{ formatNodeAccess(inventory.nodes[node]) }} →
            </template>
            {{ formatNodeAccess(preview.nodes[node]) }}
          </li>
        </ul>
        <p v-if="!changed">{{ t('nodeAccess.noChanges') }}</p>
      </div>
      <details v-if="Object.keys(inventory.nodes).length" class="preview">
        <summary>{{ t('nodeAccess.saved') }}</summary>
        <ul class="saved-nodes">
          <li v-for="(entry, node) in inventory.nodes" :key="node">
            <span
              ><strong>{{ node }}</strong> {{ formatNodeAccess(entry) }}</span
            >
            <el-button size="small" text type="danger" :disabled="busy" @click="remove(node)">
              {{ t('nodeAccess.remove') }}
            </el-button>
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
      >
        {{ t('nodeAccess.validate') }}
      </el-button>
      <el-button v-else type="primary" :loading="busy" :disabled="!changed" @click="save">
        {{ t('nodeAccess.save') }}
      </el-button>
    </div>
  </section>
</template>

<script setup>
import { computed, defineAsyncComponent, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
import { useHelpers } from '../utils/helpers'
import { formatNodeAccess } from '../utils/nodeAccess'

const props = defineProps({ botId: { type: Number, required: true } })
const emit = defineEmits(['changed', 'close'])
const JsonMappingEditor = defineAsyncComponent(() => import('./JsonMappingEditor.vue'))
const { t } = useI18n()
const { copyText } = useHelpers()
const busy = ref(false)
const error = ref('')
const input = ref('')
const inventory = ref(null)
const preview = ref(null)
const path = computed(() => `/bots/${props.botId}/node-access`)
const changed = computed(() => preview.value?.added.length + preview.value?.changed.length > 0)
const requestOptions = { _silent: true }
const mappingOptions = {
  ...requestOptions,
  headers: { 'Content-Type': 'application/json' },
  transformRequest: [(body) => body],
}
const example = [
  '{',
  '  "version": 2,',
  '  "nodes": {',
  '    "node-01": { "host": "compute-01.example.test", "ip": "192.0.2.41", "port": 2222 },',
  '    "node-02": { "host": "192.0.2.42", "port": 2222 }',
  '  }',
  '}',
].join('\n')

function copyPrompt() {
  copyText(
    t('nodeAccess.prompt', {
      nodeIds: JSON.stringify(inventory.value.node_ids),
      example,
    })
  )
}

async function run(action, message) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await action()
  } catch (failure) {
    const detail = failure.response?.data?.detail
    error.value =
      typeof detail === 'string' && failure.response?.status === 422 ? detail : t(message)
  } finally {
    busy.value = false
  }
}

async function load() {
  inventory.value = (await api.get(path.value, requestOptions)).data
}

onMounted(() => run(load, 'nodeAccess.readFailed'))

function validate() {
  try {
    JSON.parse(input.value)
  } catch {
    error.value = t('jsonEditor.invalidSyntax')
    return
  }
  return run(async () => {
    preview.value = (await api.post(`${path.value}/preview`, input.value, mappingOptions)).data
  }, 'nodeAccess.validationFailed')
}

function onEdit() {
  preview.value = null
  error.value = ''
}

function save() {
  return run(async () => {
    await api.put(path.value, input.value, mappingOptions)
    emit('changed')
    emit('close')
    ElMessage.success(t('nodeAccess.saveSuccess'))
  }, 'nodeAccess.writeFailed')
}

async function remove(node) {
  try {
    await ElMessageBox.confirm(t('nodeAccess.removeConfirm', { node }), t('nodeAccess.title'), {
      type: 'warning',
      confirmButtonText: t('nodeAccess.remove'),
      cancelButtonText: t('common.cancel'),
    })
  } catch {
    return
  }
  return run(async () => {
    await api.delete(`${path.value}/${encodeURIComponent(node)}`, requestOptions)
    emit('changed')
    preview.value = null
    await load()
  }, 'nodeAccess.writeFailed')
}
</script>

<style scoped>
.actions {
  margin-top: 20px;
  text-align: right;
}
.mapping {
  display: grid;
  gap: 8px;
  margin-top: 16px;
}
.mapping-header {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 12px;
}
.preview {
  margin-top: 16px;
  padding: 12px 16px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
  overflow-wrap: anywhere;
}
.preview p {
  margin: 0;
}
.saved-nodes {
  padding-left: 16px;
}
.error {
  color: var(--el-color-danger);
}
</style>
