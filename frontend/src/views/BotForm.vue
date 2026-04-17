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
            <el-input
              v-model="form.name"
              :placeholder="$t('botCreate.botNamePlaceholder')"
              :maxlength="64"
              show-word-limit
            />
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
        <el-col :span="10">
          <el-form-item :label="$t('botCreate.platform')" prop="platform">
            <el-select v-model="form.platform" :disabled="isEdit" style="width: 100%">
              <el-option v-for="p in availablePlatforms" :key="p" :label="p" :value="p" />
            </el-select>
          </el-form-item>
        </el-col>
      </el-row>

      <el-divider />

      <!-- Cluster Config -->
      <NodeBotForm v-if="form.bot_type === 'NODE'" v-model="clusterConfig" />
      <DeviceBotForm v-else-if="form.bot_type === 'DEVICE'" v-model="clusterConfig" />
      <QueueBotForm v-else-if="form.bot_type === 'QUEUE'" v-model="clusterConfig" />

      <ClusterPreview :bot-type="form.bot_type" :cluster-configs="clusterConfig" />

      <el-divider />

      <!-- Credentials -->
      <div class="credentials-card">
        <el-alert type="info" :closable="false" show-icon class="credentials-alert">
          <template #title>{{ $t('botCreate.credentialsHint') }}</template>
        </el-alert>
        <el-form-item :label="$t('botCreate.webhookUrl')" prop="webhook_url">
          <template #label>
            <span>{{ $t('botCreate.webhookUrl') }}</span>
            <el-tooltip placement="top" effect="light">
              <template #content>
                <div class="help-tooltip">
                  <div class="help-title">{{ $t('botCreate.webhookHelpTitle') }}</div>
                  <div>{{ $t('botCreate.webhookHelpStep1') }}</div>
                  <div>{{ $t('botCreate.webhookHelpStep2') }}</div>
                  <div>{{ $t('botCreate.webhookHelpStep3') }}</div>
                </div>
              </template>
              <el-icon class="help-icon"><QuestionFilled /></el-icon>
            </el-tooltip>
          </template>
          <el-input v-model="form.webhook_url" :placeholder="$t('botCreate.webhookPlaceholder')" />
        </el-form-item>
        <el-form-item :label="$t('botCreate.token')" prop="token">
          <template #label>
            <span>{{ $t('botCreate.token') }}</span>
            <el-tooltip placement="top" effect="light">
              <template #content>
                <div class="help-tooltip">{{ $t('botCreate.tokenHelp') }}</div>
              </template>
              <el-icon class="help-icon"><QuestionFilled /></el-icon>
            </el-tooltip>
          </template>
          <el-input
            v-model="form.token"
            :placeholder="
              isEdit && maskedToken
                ? maskedToken
                : isEdit
                  ? $t('botCreate.leaveBlank')
                  : $t('botCreate.tokenPlaceholder')
            "
            show-password
          />
        </el-form-item>
        <el-form-item :label="$t('botCreate.aesKey')" prop="aes_key">
          <template #label>
            <span>{{ $t('botCreate.aesKey') }}</span>
            <el-tooltip placement="top" effect="light">
              <template #content>
                <div class="help-tooltip">{{ $t('botCreate.aesKeyHelp') }}</div>
              </template>
              <el-icon class="help-icon"><QuestionFilled /></el-icon>
            </el-tooltip>
          </template>
          <el-input
            v-model="form.aes_key"
            :placeholder="
              isEdit && maskedAesKey
                ? maskedAesKey
                : isEdit
                  ? $t('botCreate.leaveBlank')
                  : $t('botCreate.aesKeyPlaceholder')
            "
            show-password
          />
        </el-form-item>
      </div>

      <el-divider />

      <!-- Advanced Config -->
      <el-collapse>
        <el-collapse-item :title="$t('botCreate.advancedConfig')" name="advanced">
          <el-row :gutter="16">
            <el-col :xs="24" :sm="12">
              <el-form-item prop="cfg_default_duration">
                <template #label>
                  {{ $t('botCreate.defaultDuration') }}
                  <el-tooltip
                    :content="$t('botCreate.defaultDurationHelp')"
                    placement="top"
                    effect="light"
                  >
                    <el-icon class="help-icon"><QuestionFilled /></el-icon>
                  </el-tooltip>
                </template>
                <el-input-number
                  v-model="advancedConfig.DEFAULT_DURATION"
                  :min="60"
                  :max="604800"
                  :step="300"
                  style="width: 100%"
                />
                <div class="cfg-unit">{{ $t('botCreate.defaultDurationUnit') }}</div>
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12">
              <el-form-item prop="cfg_max_lock_duration">
                <template #label>
                  {{ $t('botCreate.maxLockDuration') }}
                  <el-tooltip
                    :content="$t('botCreate.maxLockDurationHelp')"
                    placement="top"
                    effect="light"
                  >
                    <el-icon class="help-icon"><QuestionFilled /></el-icon>
                  </el-tooltip>
                </template>
                <el-input-number
                  v-model="advancedConfig.MAX_LOCK_DURATION"
                  :min="-1"
                  :max="604800"
                  :step="3600"
                  style="width: 100%"
                />
                <div class="cfg-unit">{{ $t('botCreate.defaultDurationUnit') }}</div>
                <el-alert
                  v-if="advancedConfig.MAX_LOCK_DURATION > 86400"
                  :title="$t('botCreate.maxLockDurationWarning')"
                  type="warning"
                  :closable="false"
                  show-icon
                  style="margin-top: 6px"
                />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12">
              <el-form-item prop="cfg_time_alert">
                <template #label>
                  {{ $t('botCreate.timeAlert') }}
                  <el-tooltip
                    :content="$t('botCreate.timeAlertHelp')"
                    placement="top"
                    effect="light"
                  >
                    <el-icon class="help-icon"><QuestionFilled /></el-icon>
                  </el-tooltip>
                </template>
                <el-input-number
                  v-model="advancedConfig.TIME_ALERT"
                  :min="30"
                  :max="3600"
                  :step="60"
                  style="width: 100%"
                />
                <div class="cfg-unit">{{ $t('botCreate.timeAlertUnit') }}</div>
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12">
              <el-form-item>
                <template #label>
                  {{ $t('botCreate.language') }}
                </template>
                <el-select v-model="advancedConfig.LANGUAGE" style="width: 100%">
                  <el-option :label="$t('botCreate.langZh')" value="zh" />
                  <el-option :label="$t('botCreate.langEn')" value="en" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :xs="24">
              <el-form-item>
                <template #label>
                  {{ $t('botCreate.earlyNotify') }}
                  <el-tooltip
                    :content="$t('botCreate.earlyNotifyHelp')"
                    placement="top"
                    effect="light"
                  >
                    <el-icon class="help-icon"><QuestionFilled /></el-icon>
                  </el-tooltip>
                </template>
                <el-switch v-model="advancedConfig.EARLY_NOTIFY" />
              </el-form-item>
            </el-col>
          </el-row>
        </el-collapse-item>
      </el-collapse>

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
import { ArrowLeft, QuestionFilled } from '@element-plus/icons-vue'
import { useBotsStore } from '../stores/bots'
import api from '../utils/api'
import NodeBotForm from '../components/BotForm/NodeBotForm.vue'
import DeviceBotForm from '../components/BotForm/DeviceBotForm.vue'
import QueueBotForm from '../components/BotForm/QueueBotForm.vue'
import ClusterPreview from '../components/BotForm/ClusterPreview.vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const botsStore = useBotsStore()

const isEdit = computed(() => !!route.params.id)
const formRef = ref()
const saving = ref(false)
const bot = ref(null)
const availablePlatforms = ref(['Infoflow'])
const maskedAesKey = ref('')
const maskedToken = ref('')
const nodeClusterConfig = ref({})
const deviceClusterConfig = ref({})
const clusterConfig = computed({
  get: () => (form.bot_type === 'DEVICE' ? deviceClusterConfig.value : nodeClusterConfig.value),
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
// Advanced runtime config — maps to Bot.config_overrides
const advancedConfig = reactive({
  DEFAULT_DURATION: 7200,
  MAX_LOCK_DURATION: -1,
  TIME_ALERT: 300,
  EARLY_NOTIFY: false,
  LANGUAGE: 'zh',
})

const rules = computed(() => ({
  name: [{ required: true, message: () => t('botCreate.nameRequired'), trigger: 'blur' }],
  bot_type: [{ required: true, message: () => t('botCreate.typeRequired'), trigger: 'change' }],
  webhook_url: [
    { required: true, message: () => t('botCreate.webhookRequired'), trigger: 'blur' },
    { type: 'url', message: () => t('botCreate.webhookInvalid'), trigger: 'blur' },
  ],
  aes_key: [
    { required: !isEdit.value, message: () => t('botCreate.aesKeyRequired'), trigger: 'blur' },
  ],
  token: [
    { required: !isEdit.value, message: () => t('botCreate.tokenRequired'), trigger: 'blur' },
  ],
  cfg_default_duration: [
    {
      validator: (_, val, cb) => (val >= 60 && val <= 604800 ? cb() : cb(new Error('60 ~ 604800'))),
      trigger: 'change',
    },
  ],
  cfg_max_lock_duration: [
    {
      validator: (_, val, cb) =>
        val === -1 || (val >= 300 && val <= 604800) ? cb() : cb(new Error('-1 或 300 ~ 604800')),
      trigger: 'change',
    },
  ],
  cfg_time_alert: [
    {
      validator: (_, val, cb) => (val >= 30 && val <= 3600 ? cb() : cb(new Error('30 ~ 3600'))),
      trigger: 'change',
    },
  ],
}))

onMounted(async () => {
  // Load available platforms
  try {
    const res = await api.get('/platforms')
    if (res.data.platforms?.length) {
      availablePlatforms.value = res.data.platforms
      if (!isEdit.value) form.platform = res.data.platforms[0]
    }
  } catch {
    // keep default ['Infoflow']
  }

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
      // Load config_overrides into advancedConfig
      try {
        const overrides =
          typeof bot.value.config_overrides === 'string'
            ? JSON.parse(bot.value.config_overrides)
            : bot.value.config_overrides || {}
        if (overrides.DEFAULT_DURATION != null)
          advancedConfig.DEFAULT_DURATION = overrides.DEFAULT_DURATION
        if (overrides.MAX_LOCK_DURATION != null)
          advancedConfig.MAX_LOCK_DURATION = overrides.MAX_LOCK_DURATION
        if (overrides.TIME_ALERT != null) advancedConfig.TIME_ALERT = overrides.TIME_ALERT
        if (overrides.EARLY_NOTIFY != null) advancedConfig.EARLY_NOTIFY = overrides.EARLY_NOTIFY
        if (overrides.LANGUAGE != null) advancedConfig.LANGUAGE = overrides.LANGUAGE
      } catch {
        // keep defaults
      }
      try {
        const parsed =
          typeof bot.value.cluster_configs === 'string'
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
  try {
    await formRef.value.validate()
  } catch {
    return
  }

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
    // Always include advanced config_overrides
    data.config_overrides = { ...advancedConfig }
    if (isEdit.value) {
      if (!data.aes_key) delete data.aes_key
      if (!data.token) delete data.token
      const cc = clusterConfig.value
      if (cc && Object.keys(cc).length > 0) data.cluster_configs = cc
      const needRestart = bot.value.status !== 'stopped'
      if (needRestart) {
        try {
          await botsStore.stopBot(bot.value.id)
        } catch {
          /* non-blocking */
        }
      }
      await botsStore.updateBot(bot.value.id, data)
      ElMessage.success(t('common.success'))
      if (needRestart) {
        try {
          await botsStore.startBot(bot.value.id)
        } catch {
          /* non-blocking */
        }
      }
      router.push(`/bots/${bot.value.id}`)
    } else {
      data.cluster_configs = clusterConfig.value
      const created = await botsStore.createBot(data)
      ElMessage.success(t('botCreate.createSuccess'))
      if (created.status === 'stopped') {
        ElMessage.warning(t('botCreate.autoStartSkipped'))
      }
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
.credentials-alert {
  margin-bottom: 16px;
}
.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  padding-top: 24px;
}
.help-icon {
  margin-left: 4px;
  cursor: pointer;
  color: var(--lb-text-secondary);
}
.help-tooltip {
  line-height: 1.8;
}
.help-title {
  font-weight: 600;
  margin-bottom: 4px;
}
.cfg-unit {
  font-size: 12px;
  color: var(--lb-text-secondary);
  margin-top: 2px;
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
