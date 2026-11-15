<template>
  <el-button size="small" @click="open">{{ t('nodeAccess.open') }}</el-button>
  <el-dialog
    v-model="visible"
    :title="t('nodeAccess.title')"
    width="min(620px, calc(100vw - 32px))"
  >
    <p>{{ t('nodeAccess.help') }}</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <template v-if="inventory">
      <el-button size="small" @click="copyPrompt">{{ t('nodeAccess.copyPrompt') }}</el-button>
      <p class="format-hint">
        {{ t('nodeAccess.inventoryFormat') }} <code>node-02,192.0.2.42,2222</code>
      </p>
      <label class="mapping">
        {{ t('nodeAccess.mapping') }}
        <span class="format-hint">{{ t('nodeAccess.addressFormat') }}</span>
        <el-input
          v-model="input"
          type="textarea"
          :rows="7"
          :disabled="busy"
          :maxlength="131072"
          :placeholder="example"
          @input="preview = null"
        />
      </label>
      <div v-if="preview" role="status" class="preview">
        <p>{{ t('nodeAccess.review') }}</p>
        <ul>
          <li v-for="node in [...preview.added, ...preview.changed]" :key="node">
            <strong>{{ node }}</strong>
            <template v-if="inventory.nodes[node]">
              {{ address(inventory.nodes[node]) }} →
            </template>
            {{ address(preview.nodes[node]) }}
          </li>
        </ul>
        <p v-if="!changed">{{ t('nodeAccess.noChanges') }}</p>
      </div>
      <details v-if="Object.keys(inventory.nodes).length" class="preview">
        <summary>{{ t('nodeAccess.saved') }}</summary>
        <ul class="saved-nodes">
          <li v-for="(entry, node) in inventory.nodes" :key="node">
            <span
              ><strong>{{ node }}</strong> {{ address(entry) }}</span
            >
            <el-button size="small" text type="danger" :disabled="busy" @click="remove(node)">
              {{ t('nodeAccess.remove') }}
            </el-button>
          </li>
        </ul>
      </details>
    </template>
    <template #footer>
      <el-button :disabled="busy" @click="visible = false">{{ t('common.cancel') }}</el-button>
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
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
import { useHelpers } from '../utils/helpers'

const props = defineProps({ botId: { type: Number, required: true } })
const { t } = useI18n()
const { copyText } = useHelpers()
const visible = ref(false)
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
  '  "version": 1,',
  '  "nodes": {',
  '    "node-01": { "host": "compute-01.example.test" },',
  '    "node-02": { "host": "192.0.2.42", "port": 2222 }',
  '  }',
  '}',
].join('\n')

function address({ host, port }) {
  return `${host.includes(':') ? `[${host}]` : host}:${port}`
}

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

function open() {
  input.value = ''
  inventory.value = preview.value = null
  visible.value = true
  return run(load, 'nodeAccess.readFailed')
}

function validate() {
  return run(async () => {
    preview.value = (await api.post(`${path.value}/preview`, input.value, mappingOptions)).data
  }, 'nodeAccess.validationFailed')
}

function save() {
  return run(async () => {
    await api.put(path.value, input.value, mappingOptions)
    visible.value = false
    input.value = ''
    preview.value = null
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
    preview.value = null
    await load()
  }, 'nodeAccess.writeFailed')
}
</script>

<style scoped>
.mapping {
  display: grid;
  gap: 8px;
  margin-top: 16px;
}
.format-hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  overflow-wrap: anywhere;
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
