<template>
  <details class="node-import">
    <summary>{{ t('nodeInformation.manualImport') }}</summary>
    <div class="import-toolbar">
      <el-button text size="small" @click="showExample = !showExample">
        {{ t(showExample ? 'nodeInformation.hideExample' : 'nodeInformation.showExample') }}
      </el-button>
      <el-button
        text
        size="small"
        :disabled="Boolean(input.trim()) || busy"
        @click="input = example"
      >
        {{ t('nodeInformation.insertExample') }}
      </el-button>
    </div>
    <pre v-if="showExample" class="json-example">{{ example }}</pre>
    <JsonMappingEditor
      v-model="input"
      :label="t('nodeInformation.mapping')"
      :example="example"
      :disabled="busy"
      @update:model-value="resetPreview"
    />
    <p v-if="error" role="alert">{{ t(error) }}</p>
    <p v-if="changes && !Object.keys(changes).length" role="status">
      {{ t('nodeAccess.noChanges') }}
    </p>
    <section v-else-if="changes" aria-live="polite">
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
      <el-button :disabled="busy" @click="emit('close')">{{ t('common.cancel') }}</el-button>
      <el-button
        v-if="!changes"
        type="primary"
        :loading="busy"
        :disabled="!input.trim()"
        @click="validate"
      >
        {{ t('nodeAccess.validate') }}
      </el-button>
      <el-button
        v-else
        type="primary"
        :loading="busy"
        :disabled="!Object.keys(changes).length"
        @click="save"
      >
        {{ t('nodeAccess.save') }}
      </el-button>
    </div>
  </details>
</template>

<script setup>
import { computed, defineAsyncComponent, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
import { formatNodeAccess } from '../utils/nodeAccess'

const JsonMappingEditor = defineAsyncComponent(() => import('./JsonMappingEditor.vue'))
const props = defineProps({ botId: { type: Number, required: true }, canEditHardware: Boolean })
const emit = defineEmits(['changed', 'close'])
const { t } = useI18n()
const input = ref('')
const showExample = ref(false)
const busy = ref(false)
const error = ref('')
const changes = ref(null)
const path = computed(() => `/bots/${props.botId}/node-inventory`)
const example = computed(() => {
  const fields = ['      "login": { "host": "node-01.example.test", "port": 22 }']
  if (props.canEditHardware) {
    fields.push('      "hardware": [{ "model": "P900", "count": 4 }]')
  }
  return [
    '{',
    '  "version": 1,',
    '  "nodes": {',
    '    "node-01": {',
    fields.join(',\n'),
    '    }',
    '  }',
    '}',
  ].join('\n')
})

function loginText(login) {
  return login ? formatNodeAccess(login) : '—'
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
    error.value = saveChanges ? 'nodeAccess.writeFailed' : 'nodeInformation.validationFailed'
  } finally {
    busy.value = false
  }
}

const validate = () => submit(false)
const save = () => submit(true)
</script>

<style scoped>
.node-import {
  margin-top: 16px;
}

.node-import > summary {
  cursor: pointer;
}

.import-toolbar {
  margin: 8px 0;
}

.json-example {
  background: var(--el-fill-color-light);
  padding: 10px;
  overflow-x: auto;
}

.import-actions {
  margin-top: 16px;
  text-align: right;
}
</style>
