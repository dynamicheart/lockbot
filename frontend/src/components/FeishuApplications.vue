<template>
  <el-card class="feishu-applications">
    <template #header>
      <div class="panel-heading">
        <span>{{ $t('feishu.applications') }}</span>
        <el-button @click="dialog = true">{{ $t('feishu.add') }}</el-button>
      </div>
    </template>
    <el-table v-loading="loading" :data="applications" :empty-text="$t('feishu.none')">
      <el-table-column prop="name" :label="$t('feishu.name')" />
      <el-table-column prop="app_id" label="App ID" min-width="190" />
      <el-table-column :label="$t('feishu.transport')" width="150">
        <template #default="{ row }">{{
          row.transport === 'websocket' ? $t('feishu.websocket') : 'HTTP'
        }}</template>
      </el-table-column>
      <el-table-column width="150">
        <template #default="{ row }">
          <el-button
            v-if="row.transport === 'websocket'"
            link
            type="primary"
            @click="connect(row.id)"
            >{{ $t('feishu.connect') }}</el-button
          >
        </template>
      </el-table-column>
    </el-table>
    <el-dialog
      v-model="dialog"
      :title="$t('feishu.add')"
      width="min(560px, 94vw)"
      @closed="clearSecrets"
    >
      <el-form ref="formRef" :model="form" label-position="top" @submit.prevent="save">
        <el-form-item :label="$t('feishu.name')" prop="name" :rules="required"
          ><el-input v-model="form.name" maxlength="128"
        /></el-form-item>
        <el-form-item label="App ID" prop="app_id" :rules="required"
          ><el-input v-model="form.app_id" placeholder="cli_…" autocomplete="off"
        /></el-form-item>
        <el-form-item label="App Secret" prop="app_secret" :rules="required"
          ><el-input
            v-model="form.app_secret"
            type="password"
            show-password
            autocomplete="new-password"
        /></el-form-item>
        <el-form-item :label="$t('feishu.transport')">
          <el-radio-group v-model="form.transport">
            <el-radio-button value="websocket">{{ $t('feishu.websocket') }}</el-radio-button>
            <el-radio-button value="http">HTTP</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <template v-if="form.transport === 'http'">
          <el-form-item :label="$t('feishu.tenant')" prop="tenant_key" :rules="required"
            ><el-input v-model="form.tenant_key"
          /></el-form-item>
          <el-form-item label="Verification Token" prop="verification_token" :rules="required"
            ><el-input
              v-model="form.verification_token"
              type="password"
              show-password
              autocomplete="new-password"
          /></el-form-item>
          <el-form-item label="Encrypt Key"
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
  </el-card>
</template>

<script setup>
import { onMounted, reactive, ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import api from '../utils/api'

const { t } = useI18n()
const applications = ref([])
const loading = ref(false)
const saving = ref(false)
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
async function connect(id) {
  await api.post(`/feishu/applications/${id}/connect`)
  ElMessage.success(t('feishu.connecting'))
}
onMounted(load)
</script>

<style scoped>
.feishu-applications {
  margin-top: 20px;
}
.panel-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
</style>
