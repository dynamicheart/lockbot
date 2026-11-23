<template>
  <div>
    <template v-if="!changes">
      <div class="import-toolbar">
        <span>{{ t('nodeInformation.importHint') }}</span>
        <el-button size="small" :disabled="Boolean(input.trim()) || busy" @click="input = example">
          {{ t('nodeInformation.insertExample') }}
        </el-button>
      </div>
      <JsonMappingEditor
        v-model="input"
        :label="t('nodeInformation.mapping')"
        :rows="8"
        :disabled="busy"
        @update:model-value="error = ''"
      />
    </template>
    <p v-if="error" role="alert">{{ t(error) }}</p>
    <p v-if="changes && !Object.keys(changes).length" role="status">
      {{ t('nodeAccess.noChanges') }}
    </p>
    <section v-else-if="changes" class="import-review" aria-live="polite">
      <p>{{ t('nodeAccess.review') }}</p>
      <ul>
        <li v-for="(change, node) in changes" :key="node">
          <strong>{{ node }}</strong>
          <div v-if="Object.hasOwn(change.after, 'login')">
            {{ t('nodeAccess.loginAddress') }}
            {{ loginText(change.before.login) }} → {{ loginText(change.after.login) }}
          </div>
          <div v-if="Object.hasOwn(change.after, 'hardware')">
            {{ t('nodeHardware.title') }}
            {{ hardwareText(change.before.hardware) }} → {{ hardwareText(change.after.hardware) }}
          </div>
        </li>
      </ul>
    </section>
    <div class="import-actions">
      <el-button v-if="changes" :disabled="busy" @click="resetPreview">
        {{ t('nodeInformation.backToEdit') }}
      </el-button>
      <el-button v-else :disabled="busy" @click="emit('close')">{{ t('common.cancel') }}</el-button>
      <el-button
        v-if="!changes"
        type="primary"
        :loading="busy"
        :disabled="!input.trim()"
        @click="validate"
      >
        {{ t('nodeInformation.previewChanges') }}
      </el-button>
      <el-button
        v-else
        type="primary"
        :loading="busy"
        :disabled="!Object.keys(changes).length"
        @click="save"
      >
        {{ t('common.save') }}
      </el-button>
    </div>
  </div>
</template>

<script setup>
import { computed, defineAsyncComponent, onBeforeUnmount, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
import { formatNodeAccess } from '../utils/nodeAccess'

const JsonMappingEditor = defineAsyncComponent(() => import('./JsonMappingEditor.vue'))
const props = defineProps({ botId: { type: Number, required: true }, canEditHardware: Boolean })
const emit = defineEmits(['changed', 'close'])
const { t } = useI18n()
const input = ref('')
const busy = ref(false)
const error = ref('')
const changes = ref(null)
const controller = new AbortController()
onBeforeUnmount(() => controller.abort())
const path = computed(() => `/bots/${props.botId}/node-inventory`)
const example = computed(() => {
  const method = JSON.stringify(t('nodeInformation.exampleLoginMethod'))
  const note = JSON.stringify(t('nodeInformation.exampleLoginNote'))
  const hardware = props.canEditHardware
    ? ',\n      "hardware": [{ "model": "P900", "count": 4 }]'
    : ''
  return `{
  "version": 1,
  "nodes": {
    "node-01": {
      "host": "node-01.example.test",
      "ip": "192.0.2.41",
      "port": 22,
      "method": ${method},
      "note": ${note}${hardware}
    }
  }
}`
})

function loginText(login) {
  return login
    ? [formatNodeAccess(login), login.method, login.note].filter(Boolean).join(' · ')
    : '—'
}

function hardwareText(cards) {
  return cards?.map(({ model, count }) => `${model} × ${count}`).join(' · ') || '—'
}

function resetPreview() {
  changes.value = null
  error.value = ''
}

async function submit(saveChanges) {
  if (busy.value) return
  error.value = ''
  try {
    JSON.parse(input.value)
  } catch {
    error.value = 'jsonEditor.invalidSyntax'
    return
  }
  busy.value = true
  const options = {
    _silent: true,
    signal: controller.signal,
    headers: { 'Content-Type': 'application/json' },
    transformRequest: [(value) => value],
  }
  try {
    if (saveChanges) {
      await api.patch(path.value, input.value, options)
      emit('changed')
      emit('close')
      ElMessage.success(t('nodeInformation.saved'))
    } else {
      const { data } = await api.post(path.value + '/preview', input.value, options)
      changes.value = data.changes
    }
  } catch {
    if (!controller.signal.aborted) {
      error.value = saveChanges ? 'nodeAccess.writeFailed' : 'nodeInformation.validationFailed'
    }
  } finally {
    busy.value = false
  }
}

const validate = () => submit(false)
const save = () => submit(true)
</script>

<style scoped>
.import-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.import-review {
  max-height: 290px;
  overflow-y: auto;
  overflow-wrap: anywhere;
}

.import-actions {
  margin-top: 16px;
  text-align: right;
}
</style>
