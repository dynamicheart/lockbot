<template>
  <div class="bot-form-page">
    <div class="form-header">
      <el-button text @click="$router.back()">
        <el-icon><ArrowLeft /></el-icon> {{ $t('botDetail.back') }}
      </el-button>
      <h2 class="form-title">{{ isEdit ? $t('botDetail.editConfig') : $t('botCreate.title') }}</h2>
    </div>

    <el-alert
      v-if="isEdit && bot?.status === 'running'"
      :title="$t('botDetail.saveWillRestart')"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 20px"
    />

    <el-form
      ref="formRef"
      :model="form"
      :rules="rules"
      label-position="top"
      class="bot-form-body"
      @submit.prevent="handleSubmit"
    >
      <!-- Basic Info -->
      <el-row :gutter="16">
        <el-col :span="14">
          <el-form-item :label="$t('botCreate.botName')" prop="name">
            <el-input v-model="form.name" :placeholder="$t('botCreate.botNamePlaceholder')" :maxlength="64" show-word-limit />
          </el-form-item>
        </el-col>
        <el-col :span="10">
          <el-form-item :label="$t('botCreate.botType')" prop="bot_type">
            <el-select v-model="form.bot_type" :disabled="isEdit" style="width: 100%">
              <el-option label="NODE" value="NODE" />
              <el-option label="DEVICE" value="DEVICE" />
              <el-option label="QUEUE" value="QUEUE" />
            </el-select>
          </el-form-item>
        </el-col>
      </el-row>

      <el-divider />

      <!-- Cluster Config -->
      <NodeBotForm v-if="form.bot_type === 'NODE'" v-model="clusterConfig" />
      <DeviceBotForm v-else-if="form.bot_type === 'DEVICE'" v-model="clusterConfig" />
      <QueueBotForm v-else-if="form.bot_type === 'QUEUE'" v-model="clusterConfig" />

      <el-divider />

      <!-- Credentials -->
      <div class="credentials-card">
        <el-form-item :label="$t('botCreate.webhookUrl')" prop="webhook_url">
          <el-input v-model="form.webhook_url" :placeholder="$t('botCreate.webhookPlaceholder')" />
        </el-form-item>
        <el-form-item :label="$t('botCreate.aesKey')" prop="aes_key">
          <el-input
            v-model="form.aes_key"
            :placeholder="isEdit && maskedAesKey ? maskedAesKey : (isEdit ? $t('botCreate.leaveBlank') : $t('botCreate.aesKeyPlaceholder'))"
            show-password
          />
        </el-form-item>
        <el-form-item :label="$t('botCreate.token')" prop="token">
          <el-input
            v-model="form.token"
            :placeholder="isEdit && maskedToken ? maskedToken : (isEdit ? $t('botCreate.leaveBlank') : $t('botCreate.tokenPlaceholder'))"
            show-password
          />
        </el-form-item>
      </div>

      <!-- Actions -->
      <div class="form-actions">
        <el-button @click="$router.back()">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="handleSubmit">
          {{ isEdit ? $t('common.save') : $t('common.create') }}
        </el-button>
      </div>
    </el-form>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { ArrowLeft } from '@element-plus/icons-vue'
import { useBotsStore } from '../stores/bots'
import NodeBotForm from '../components/BotForm/NodeBotForm.vue'
import DeviceBotForm from '../components/BotForm/DeviceBotForm.vue'
import QueueBotForm from '../components/BotForm/QueueBotForm.vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const botsStore = useBotsStore()

const isEdit = computed(() => !!route.params.id)
const formRef = ref()
const saving = ref(false)
const bot = ref(null)
const maskedAesKey = ref('')
const maskedToken = ref('')
const nodeClusterConfig = ref({})
const deviceClusterConfig = ref({})
const clusterConfig = computed({
  get: () => form.bot_type === 'DEVICE' ? deviceClusterConfig.value : nodeClusterConfig.value,
  set: (v) => {
    if (form.bot_type === 'DEVICE') deviceClusterConfig.value = v
    else nodeClusterConfig.value = v
  },
})

const form = reactive({
  name: '',
  bot_type: 'NODE',
  platform: 'Infoflow',
  webhook_url: '',
  aes_key: '',
  token: '',
})

const rules = computed(() => ({
  name: [{ required: true, message: () => t('botCreate.nameRequired'), trigger: 'blur' }],
  bot_type: [{ required: true, message: () => t('botCreate.typeRequired'), trigger: 'change' }],
  webhook_url: [
    { required: true, message: () => t('botCreate.webhookRequired'), trigger: 'blur' },
    { type: 'url', message: () => t('botCreate.webhookInvalid'), trigger: 'blur' },
  ],
  aes_key: [{ required: !isEdit.value, message: () => t('botCreate.aesKeyRequired'), trigger: 'blur' }],
  token: [{ required: !isEdit.value, message: () => t('botCreate.tokenRequired'), trigger: 'blur' }],
}))

onMounted(async () => {
  if (isEdit.value) {
    try {
      bot.value = await botsStore.getBot(route.params.id)
      form.name = bot.value.name
      form.bot_type = bot.value.bot_type
      form.platform = bot.value.platform
      form.webhook_url = bot.value.webhook_url_raw || ''
      form.aes_key = ''
      form.token = ''
      maskedAesKey.value = bot.value.aes_key_masked || ''
      maskedToken.value = bot.value.token_masked || ''
      try {
        const parsed = typeof bot.value.cluster_configs === 'string'
          ? JSON.parse(bot.value.cluster_configs)
          : bot.value.cluster_configs || {}
        if (form.bot_type === 'DEVICE') deviceClusterConfig.value = parsed
        else nodeClusterConfig.value = parsed
      } catch {
        if (form.bot_type === 'DEVICE') deviceClusterConfig.value = {}
        else nodeClusterConfig.value = {}
      }
    } catch {
      ElMessage.error(t('botForm.notFound'))
      router.replace('/bots')
    }
  }
})

async function handleSubmit() {
  try { await formRef.value.validate() } catch { return }

  if (!isEdit.value) {
    const cc = clusterConfig.value
    const nodeCount = cc ? Object.keys(cc).length : 0
    if (nodeCount === 0) {
      ElMessage.error(t('botCreate.clusterRequired'))
      return
    }
    if (form.bot_type === 'DEVICE') {
      const totalDevs = Object.values(cc).flat().length
      if (totalDevs === 0) {
        ElMessage.error(t('botForm.deviceRequired'))
        return
      }
    }
  } else {
    const cc = clusterConfig.value
    if (cc && Object.keys(cc).length > 0 && form.bot_type === 'DEVICE') {
      const totalDevs = Object.values(cc).flat().length
      if (totalDevs === 0) {
        ElMessage.error(t('botForm.deviceRequired'))
        return
      }
    }
  }

  saving.value = true
  try {
    const data = { ...form }
    if (isEdit.value) {
      if (!data.aes_key) delete data.aes_key
      if (!data.token) delete data.token
      const cc = clusterConfig.value
      if (cc && Object.keys(cc).length > 0) data.cluster_configs = cc
      const needRestart = bot.value.status !== 'stopped'
      if (needRestart) {
        try { await botsStore.stopBot(bot.value.id) } catch { /* non-blocking */ }
      }
      await botsStore.updateBot(bot.value.id, data)
      ElMessage.success(t('common.success'))
      if (needRestart) {
        try { await botsStore.startBot(bot.value.id) } catch { /* non-blocking */ }
      }
      router.push(`/bots/${bot.value.id}`)
    } else {
      data.cluster_configs = clusterConfig.value
      const created = await botsStore.createBot(data)
      ElMessage.success(t('botCreate.createSuccess'))
      await botsStore.fetchBots()
      router.push(`/bots/${created.id}`)
    }
  } catch {
    // handled by interceptor
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.bot-form-page {
  max-width: 720px;
  margin: 0 auto;
  padding: 0 20px;
}
.form-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 24px;
}
.form-title {
  font-size: 20px;
  font-weight: 600;
  color: var(--lb-text-primary);
  margin: 0;
}
.bot-form-body {
  background: var(--el-bg-color);
  border: 1px solid var(--lb-border-light);
  border-radius: 12px;
  padding: 28px 32px;
}
.credentials-card {
  background: var(--el-fill-color-lighter);
  border-radius: 8px;
  padding: 16px 20px 4px;
  border: 1px solid var(--lb-border-light);
}
.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  padding-top: 24px;
}
@media (max-width: 768px) {
  .bot-form-page {
    padding: 0 8px;
  }
  .bot-form-body {
    padding: 16px;
  }
}
</style>
