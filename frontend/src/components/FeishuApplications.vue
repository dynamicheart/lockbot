<template>
  <section class="feishu-applications">
    <div class="panel-heading">
      <h3>{{ $t('feishu.applications') }}</h3>
      <el-button @click="dialog = true">{{ $t('feishu.add') }}</el-button>
    </div>
    <el-table v-loading="loading" :data="applications" :empty-text="$t('feishu.none')">
      <el-table-column prop="name" :label="$t('feishu.name')" />
      <el-table-column prop="app_id" label="App ID" min-width="190" />
      <el-table-column :label="$t('feishu.transport')" width="150">
        <template #default="{ row }">{{
          row.transport === 'websocket' ? $t('feishu.websocket') : 'HTTP'
        }}</template>
      </el-table-column>
      <el-table-column :label="$t('feishu.receiverStatus')" min-width="180">
        <template #default="{ row }">
          <el-tooltip
            v-if="row.transport === 'websocket'"
            :content="receiverHelp(row)"
            effect="light"
          >
            <span>{{ $t(`feishu.receiverStates.${receiverState(row)}`) }}</span>
          </el-tooltip>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column width="150">
        <template #default="{ row }">
          <el-button
            v-if="row.transport === 'websocket'"
            link
            type="primary"
            :loading="!!operations[row.id]"
            @click="receiverAction(row)"
            >{{
              $t(receiverState(row) === 'stopped' ? 'feishu.connect' : 'feishu.refreshReceiver')
            }}</el-button
          >
        </template>
      </el-table-column>
    </el-table>
    <el-dialog
      v-model="dialog"
      :title="$t('feishu.add')"
      width="min(560px, 94vw)"
      class="feishu-application-dialog"
      @closed="clearSecrets"
    >
      <p class="application-guide">
        <a
          href="https://github.com/dynamicheart/lockbot/blob/release/2.x/docs/usage/feishu.md"
          target="_blank"
          rel="noopener noreferrer"
          >{{ $t('feishu.setupGuide') }}</a
        >
      </p>
      <el-form
        ref="formRef"
        :model="form"
        :validate-on-rule-change="false"
        label-position="top"
        @submit.prevent="save"
      >
        <el-form-item :label="$t('feishu.name')" prop="name" :rules="required">
          <template #label
            >{{ $t('feishu.name')
            }}<el-tooltip :content="$t('feishu.nameHelp')" placement="top" effect="light"
              ><el-icon class="field-help"><QuestionFilled /></el-icon></el-tooltip></template
          ><el-input v-model="form.name" maxlength="128"
        /></el-form-item>
        <el-form-item label="App ID" prop="app_id" :rules="required">
          <template #label
            >App ID<el-tooltip :content="$t('feishu.appIdHelp')" placement="top" effect="light"
              ><el-icon class="field-help"><QuestionFilled /></el-icon></el-tooltip></template
          ><el-input v-model="form.app_id" placeholder="cli_…" autocomplete="off"
        /></el-form-item>
        <el-form-item label="App Secret" prop="app_secret" :rules="required">
          <template #label
            >App Secret<el-tooltip
              :content="$t('feishu.appSecretHelp')"
              placement="top"
              effect="light"
              ><el-icon class="field-help"><QuestionFilled /></el-icon></el-tooltip></template
          ><el-input
            v-model="form.app_secret"
            type="password"
            show-password
            autocomplete="new-password"
        /></el-form-item>
        <el-form-item :label="$t('feishu.transport')">
          <template #label
            >{{ $t('feishu.transport')
            }}<el-tooltip :content="$t('feishu.transportHelp')" placement="top" effect="light"
              ><el-icon class="field-help"><QuestionFilled /></el-icon></el-tooltip
          ></template>
          <el-radio-group v-model="form.transport">
            <el-radio-button value="websocket">{{ $t('feishu.websocket') }}</el-radio-button>
            <el-radio-button value="http">HTTP</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <template v-if="form.transport === 'http'">
          <el-form-item :label="$t('feishu.tenant')" prop="tenant_key" :rules="required">
            <template #label
              >{{ $t('feishu.tenant')
              }}<el-tooltip :content="$t('feishu.tenantHelp')" placement="top" effect="light"
                ><el-icon class="field-help"><QuestionFilled /></el-icon></el-tooltip></template
            ><el-input v-model="form.tenant_key"
          /></el-form-item>
          <el-form-item label="Verification Token" prop="verification_token" :rules="required">
            <template #label
              >Verification Token<el-tooltip
                :content="$t('feishu.verificationHelp')"
                placement="top"
                effect="light"
                ><el-icon class="field-help"><QuestionFilled /></el-icon></el-tooltip></template
            ><el-input
              v-model="form.verification_token"
              type="password"
              show-password
              autocomplete="new-password"
          /></el-form-item>
          <el-form-item label="Encrypt Key">
            <template #label
              >Encrypt Key<el-tooltip
                :content="$t('feishu.encryptHelp')"
                placement="top"
                effect="light"
                ><el-icon class="field-help"><QuestionFilled /></el-icon></el-tooltip></template
            ><el-input
              v-model="form.encrypt_key"
              type="password"
              show-password
              autocomplete="new-password"
          /></el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{
          $t('common.save')
        }}</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup>
import { onMounted, reactive, ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import api from '../utils/api'
import { QuestionFilled } from '@element-plus/icons-vue'

const { t } = useI18n()
const applications = ref([])
const loading = ref(false)
const saving = ref(false)
const operations = reactive({})
const dialog = ref(false)
const formRef = ref()
const form = reactive({
  name: '',
  app_id: '',
  app_secret: '',
  transport: 'websocket',
  tenant_key: '',
  verification_token: '',
  encrypt_key: '',
})
const required = computed(() => [
  { required: true, message: t('feishu.required'), trigger: 'blur' },
])
function clearSecrets() {
  form.app_secret = ''
  form.verification_token = ''
  form.encrypt_key = ''
}
async function load() {
  loading.value = true
  try {
    applications.value = (await api.get('/feishu/applications')).data
  } finally {
    loading.value = false
  }
}
async function save() {
  if (!(await formRef.value.validate().catch(() => false))) return
  saving.value = true
  try {
    const payload = { ...form }
    if (payload.transport === 'websocket') {
      payload.tenant_key = ''
      payload.verification_token = ''
      payload.encrypt_key = ''
    }
    await api.post('/feishu/applications', payload)
    dialog.value = false
    clearSecrets()
    ElMessage.success(t('common.success'))
    await load()
  } finally {
    saving.value = false
  }
}
function receiverState(row) {
  const state = row.receiver?.state
  return ['starting', 'running', 'retrying'].includes(state) ? state : 'stopped'
}
function receiverHelp(row) {
  const value = row.receiver?.last_handshake_at
  const time =
    value && Number.isFinite(Date.parse(value))
      ? new Date(value).toLocaleString()
      : t('feishu.noHandshake')
  return t('feishu.receiverHelp', { time })
}
async function receiverAction(row) {
  if (operations[row.id]) return
  operations[row.id] = true
  try {
    if (receiverState(row) === 'stopped') {
      await api.post(`/feishu/applications/${row.id}/connect`)
      ElMessage.success(t('feishu.connecting'))
    }
    await load()
  } finally {
    delete operations[row.id]
  }
}
onMounted(load)
</script>

<style scoped>
.field-help {
  margin-left: 5px;
  color: var(--el-text-color-secondary);
  cursor: help;
}
.application-guide {
  margin: 0 0 16px;
  font-size: 13px;
}
.application-guide a {
  color: var(--el-color-primary);
  text-decoration: none;
}
.application-guide a:hover {
  text-decoration: underline;
}

.feishu-applications {
  margin-top: 24px;
  padding-top: 20px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.panel-heading h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.panel-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
</style>

<style>
.feishu-application-dialog .el-dialog__body {
  max-height: 60vh;
  overflow-y: auto;
}
</style>
