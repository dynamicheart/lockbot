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
          <el-form-item :label="$t('botCreate.platform')">
            <el-select
              v-if="availablePlatforms.length > 1"
              v-model="form.platform"
              :disabled="isEdit"
              style="width: 100%"
            >
              <el-option v-for="p in availablePlatforms" :key="p" :label="p" :value="p" />
            </el-select>
            <el-input v-else v-model="form.platform" disabled style="width: 100%" />
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
          <template #default>
            <div>{{ credHints.title }}</div>
            <div v-for="step in credHints.steps" :key="step">{{ step }}</div>
          </template>
        </el-alert>

        <el-form-item
          v-for="fieldKey in credFieldOrder"
          :key="fieldKey"
          :prop="credFields[fieldKey]?.required ? `credentials.${fieldKey}` : undefined"
        >
          <template #label>
            <span>{{ credFields[fieldKey].label }}</span>
            <el-tooltip placement="top" effect="light" :content="fieldHints[fieldKey]">
              <el-icon class="help-icon"><QuestionFilled /></el-icon>
            </el-tooltip>
          </template>
          <el-input
            v-model="form.credentials[fieldKey]"
            :placeholder="
              credFields[fieldKey]?.isUrl
                ? credFields[fieldKey].placeholder
                : isEdit && maskedCreds[fieldKey]
                  ? maskedCreds[fieldKey]
                  : isEdit
                    ? $t('botCreate.leaveBlank')
                    : credFields[fieldKey].placeholder
            "
            :show-password="!credFields[fieldKey]?.isUrl"
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
                  v-model="form.cfg_default_duration"
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
                  v-model="form.cfg_max_lock_duration"
                  :min="-1"
                  :max="604800"
                  :step="3600"
                  style="width: 100%"
                />
                <div class="cfg-unit">{{ $t('botCreate.defaultDurationUnit') }}</div>
                <el-alert
                  v-if="form.cfg_max_lock_duration > 86400"
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
                  v-model="form.cfg_time_alert"
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
                <el-select v-model="form.cfg_language" style="width: 100%">
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
                <el-switch v-model="form.cfg_early_notify" />
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
import { ref, reactive, computed, watch, onMounted, nextTick } from 'vue'
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

const { t, te } = useI18n()
const route = useRoute()
const router = useRouter()
const botsStore = useBotsStore()

const isEdit = computed(() => !!route.params.id)
const formRef = ref()
const saving = ref(false)
const bot = ref(null)
const availablePlatforms = ref(['Infoflow'])
const maskedCreds = ref({})

const form = reactive({
  name: '',
  bot_type: 'NODE',
  platform: 'Infoflow',
  credentials: {},
  cfg_default_duration: 7200,
  cfg_max_lock_duration: -1,
  cfg_time_alert: 300,
  cfg_early_notify: false,
  cfg_language: 'zh',
})

// Per-platform credential field definitions.
// Each platform defines its own keys with labels, placeholders, and requirements.
const PLATFORM_CRED_CONFIG = {
  Infoflow: {
    token: { label: 'App Token', placeholder: 'App Token', required: true },
    aes_key: { label: 'AES Key', placeholder: 'AES Key', required: true },
    webhook_url: {
      label: 'Webhook URL',
      placeholder: 'https://example.com/webhook',
      required: true,
      isUrl: true,
    },
  },
  Slack: {
    bot_token: { label: 'Bot Token', placeholder: 'xoxb-...', required: true },
    signing_secret: { label: 'Signing Secret', placeholder: 'Signing Secret', required: true },
    event_url: {
      label: 'Event Subscription URL',
      placeholder: 'https://example.com/slack/events',
      required: false,
      isUrl: true,
    },
  },
  DingTalk: {
    app_secret: { label: 'App Secret', placeholder: 'App Secret', required: true },
  },
  Feishu: {
    app_id: { label: 'App ID', placeholder: 'App ID', required: true, isUrl: false },
    app_secret: { label: 'App Secret', placeholder: 'App Secret', required: true },
    encrypt_key: { label: 'Encrypt Key', placeholder: t('botCreate.optional'), required: false },
  },
}

// Ordered field keys for rendering (preserves UI order per platform)
const PLATFORM_CRED_ORDER = {
  Infoflow: ['webhook_url', 'token', 'aes_key'],
  Slack: ['bot_token', 'signing_secret', 'event_url'],
  DingTalk: ['app_secret'],
  Feishu: ['app_id', 'app_secret', 'encrypt_key'],
}

const credFields = computed(() => {
  return PLATFORM_CRED_CONFIG[form.platform] || PLATFORM_CRED_CONFIG['Infoflow']
})

const credFieldOrder = computed(() => {
  return PLATFORM_CRED_ORDER[form.platform] || PLATFORM_CRED_ORDER['Infoflow']
})

// Per-platform credential storage — switching platforms preserves each platform's own values
const platformCreds = reactive(
  Object.fromEntries(['Infoflow', 'Slack', 'DingTalk', 'Feishu'].map((p) => [p, {}]))
)

// When platform switches: load that platform's stored creds into form
watch(
  () => form.platform,
  (newPlatform) => {
    form.credentials = { ...(platformCreds[newPlatform] || {}) }
    nextTick(() => formRef.value?.clearValidate())
  }
)

// Auto-save current creds back to per-platform storage as user types
watch(
  () => form.credentials,
  (creds) => {
    const p = form.platform
    if (platformCreds[p]) {
      platformCreds[p] = { ...creds }
    }
  },
  { deep: true }
)

// Per-platform tooltip hints
const fieldHints = computed(() => {
  const hints = {}
  for (const key of credFieldOrder.value) {
    const i18nKey = `botCreate.credFieldHints.${form.platform}.${key}`
    hints[key] = te(i18nKey) ? t(i18nKey) : t('botCreate.tokenHelp')
  }
  return hints
})

// Credential alert: use platform-specific steps if available, else generic
const credHints = computed(() => {
  const prefix = `botCreate.credFieldHints.${form.platform}`
  if (te(`${prefix}.step1`)) {
    return {
      title: t(`${prefix}.hintTitle`),
      steps: [t(`${prefix}.step1`), t(`${prefix}.step2`), t(`${prefix}.step3`)],
    }
  }
  return { title: t('botCreate.credentialsHint'), steps: null }
})
const nodeClusterConfig = ref({})
const deviceClusterConfig = ref({})
const clusterConfig = computed({
  get: () => (form.bot_type === 'DEVICE' ? deviceClusterConfig.value : nodeClusterConfig.value),
  set: (v) => {
    if (form.bot_type === 'DEVICE') deviceClusterConfig.value = v
    else nodeClusterConfig.value = v
  },
})

const rules = computed(() => {
  const r = {
    name: [{ required: true, message: () => t('botCreate.nameRequired'), trigger: 'blur' }],
    bot_type: [{ required: true, message: () => t('botCreate.typeRequired'), trigger: 'change' }],
  }
  // Dynamic credential field rules
  for (const key of credFieldOrder.value) {
    const field = credFields.value[key]
    if (!field) continue
    const prop = `credentials.${key}`
    const validators = []
    if (field.required && !isEdit.value) {
      validators.push({
        required: true,
        message: () => t('botCreate.fieldRequired', { field: field.label }),
        trigger: 'blur',
      })
    }
    if (field.isUrl) {
      validators.push({
        validator: (_, val, cb) => {
          if (!val) return cb()
          try {
            new URL(val)
            cb()
          } catch {
            cb(new Error(t('botCreate.webhookInvalid')))
          }
        },
        trigger: 'blur',
      })
    }
    if (validators.length) r[prop] = validators
  }
  r.cfg_default_duration = [
    {
      validator: (_, val, cb) => (val >= 60 && val <= 604800 ? cb() : cb(new Error('60 ~ 604800'))),
      trigger: 'change',
    },
  ]
  r.cfg_max_lock_duration = [
    {
      validator: (_, val, cb) =>
        val === -1 || (val >= 300 && val <= 604800) ? cb() : cb(new Error('-1 或 300 ~ 604800')),
      trigger: 'change',
    },
  ]
  r.cfg_time_alert = [
    {
      validator: (_, val, cb) => (val >= 30 && val <= 3600 ? cb() : cb(new Error('30 ~ 3600'))),
      trigger: 'change',
    },
  ]
  return r
})

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
      // On edit, credentials are empty (user must re-enter); masked values shown as placeholders
      form.credentials = {}
      maskedCreds.value = bot.value.credentials_masked || {}
      // Load config_overrides into form
      try {
        const overrides =
          typeof bot.value.config_overrides === 'string'
            ? JSON.parse(bot.value.config_overrides)
            : bot.value.config_overrides || {}
        if (overrides.DEFAULT_DURATION != null)
          form.cfg_default_duration = overrides.DEFAULT_DURATION
        if (overrides.MAX_LOCK_DURATION != null)
          form.cfg_max_lock_duration = overrides.MAX_LOCK_DURATION
        if (overrides.TIME_ALERT != null) form.cfg_time_alert = overrides.TIME_ALERT
        if (overrides.EARLY_NOTIFY != null) form.cfg_early_notify = overrides.EARLY_NOTIFY
        if (overrides.LANGUAGE != null) form.cfg_language = overrides.LANGUAGE
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
    // Build config_overrides from cfg_* fields
    data.config_overrides = {
      DEFAULT_DURATION: form.cfg_default_duration,
      MAX_LOCK_DURATION: form.cfg_max_lock_duration,
      TIME_ALERT: form.cfg_time_alert,
      EARLY_NOTIFY: form.cfg_early_notify,
      LANGUAGE: form.cfg_language,
    }
    // On edit: send all credential fields including empty strings (to allow clearing optional fields)
    // On create: remove empty credential values
    if (isEdit.value) {
      const allCredKeys = credFieldOrder.value
      data.credentials = Object.fromEntries(allCredKeys.map((k) => [k, data.credentials[k] || '']))
    } else {
      data.credentials = Object.fromEntries(Object.entries(data.credentials).filter(([, v]) => v))
    }
    if (isEdit.value && Object.keys(data.credentials).length === 0) delete data.credentials
    if (isEdit.value) {
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
