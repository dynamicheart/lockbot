<template>
  <div class="bot-form-page">
    <div class="form-header">
      <el-button text @click="$router.back()">
        <el-icon><ArrowLeft /></el-icon> {{ $t('botDetail.back') }}
      </el-button>
      <h2 class="form-title">{{ isEdit ? $t('botDetail.editConfig') : $t('botCreate.title') }}</h2>
    </div>

    <el-alert
      v-if="isEdit && bot?.status === 'running' && configChanged"
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
              <el-option label="QueueDevice" value="QUEUE_DEVICE" />
            </el-select>
          </el-form-item>
        </el-col>
      </el-row>

      <el-divider />

      <el-form-item v-if="auth.isSuperAdmin || isEdit" :label="$t('feishu.platform')">
        <el-select v-model="form.platform" :disabled="isEdit" style="width: 100%">
          <el-option v-if="isEdit || platformAccess.Infoflow" label="Infoflow" value="Infoflow" />
          <el-option v-if="isEdit || platformAccess.Feishu" label="飞书 / Feishu" value="Feishu" />
        </el-select>
      </el-form-item>
      <template v-if="form.platform.toLowerCase() === 'feishu'">
        <el-form-item
          v-if="!isEdit"
          prop="feishu_application_id"
          :rules="[{ required: true, message: t('feishu.required'), trigger: 'change' }]"
        >
          <template #label>
            {{ $t('feishu.application') }}
            <el-tooltip :content="$t('feishu.applicationHelp')" placement="top" effect="light">
              <el-icon class="help-icon"><QuestionFilled /></el-icon>
            </el-tooltip>
          </template>
          <el-select
            v-model="form.feishu_application_id"
            style="width: 100%"
            @visible-change="loadFeishuApplications"
          >
            <el-option
              v-for="app in feishuApplications"
              :key="app.id"
              :label="app.name"
              :value="app.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item
          :label="$t('feishu.group')"
          prop="group_id"
          :rules="[{ required: true, message: t('feishu.required'), trigger: 'blur' }]"
        >
          <el-input v-model="form.group_id" :disabled="isEdit" placeholder="oc_…" />
          <FeishuGroupInfo
            :bot-id="isEdit ? bot?.id : null"
            :application-id="form.feishu_application_id"
            :chat-id="form.group_id"
          />
        </el-form-item>
      </template>

      <!-- Cluster Config -->
      <NodeBotForm
        v-if="form.bot_type === 'NODE'"
        v-model="clusterConfig"
        :aliases="advancedConfig.NODE_ALIASES"
        @update:has-error="clusterHasError = $event"
        @update:aliases="advancedConfig.NODE_ALIASES = $event"
      />
      <DeviceBotForm
        v-else-if="['DEVICE', 'QUEUE_DEVICE'].includes(form.bot_type)"
        ref="deviceFormRef"
        v-model="clusterConfig"
        :aliases="advancedConfig.NODE_ALIASES"
        @update:has-error="clusterHasError = $event"
        @update:aliases="advancedConfig.NODE_ALIASES = $event"
      />
      <QueueBotForm
        v-else-if="form.bot_type === 'QUEUE'"
        v-model="clusterConfig"
        :aliases="advancedConfig.NODE_ALIASES"
        @update:has-error="clusterHasError = $event"
        @update:aliases="advancedConfig.NODE_ALIASES = $event"
      />

      <ClusterPreview
        :bot-type="form.bot_type"
        :cluster-configs="clusterConfig"
        :node-aliases="advancedConfig.NODE_ALIASES"
        :show-node-alias="advancedConfig.SHOW_NODE_ALIAS"
        :display-device-model="advancedConfig.DISPLAY_DEVICE_MODEL"
        :forbid-relock="advancedConfig.FORBID_RELOCK"
        :max-resources-per-user="advancedConfig.MAX_RESOURCES_PER_USER"
      />

      <el-divider />

      <!-- Credentials -->
      <div v-if="form.platform.toLowerCase() !== 'feishu'" class="credentials-card">
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

      <section class="agent-settings" :aria-label="$t('botDetail.agentGroup')">
        <div class="agent-settings-title">{{ $t('botDetail.agentGroup') }}</div>
        <div class="agent-setting-row">
          <label class="agent-setting-label" for="agent-access">
            {{ $t('botDetail.agentAccess') }}
            <el-tooltip :content="$t('botDetail.agentAccessHelp')" placement="top">
              <el-icon class="help-icon"><QuestionFilled /></el-icon>
            </el-tooltip>
          </label>
          <el-switch
            id="agent-access"
            v-model="agentAccess"
            :disabled="isEdit && !bot"
            :aria-label="$t('botDetail.agentAccess')"
          />
        </div>
        <div class="agent-setting-row agent-notify-row" :class="{ 'is-disabled': !agentAccess }">
          <label class="agent-setting-label" for="agent-notify">
            {{ $t('botDetail.agentNotify') }}
            <el-tooltip :content="$t('botDetail.agentNotifyHelp')" placement="top">
              <el-icon class="help-icon"><QuestionFilled /></el-icon>
            </el-tooltip>
          </label>
          <el-switch
            id="agent-notify"
            v-model="agentNotify"
            :disabled="!agentAccess || (isEdit && !bot)"
            :aria-label="$t('botDetail.agentNotify')"
          />
        </div>
      </section>

      <!-- Advanced Config -->
      <el-collapse>
        <el-collapse-item :title="$t('botCreate.advancedConfig')" name="advanced">
          <el-row :gutter="16">
            <el-col :xs="24" :sm="12">
              <el-form-item>
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
                  :value-on-clear="7200"
                  style="width: 100%"
                />
                <div class="cfg-unit">{{ formatSeconds(advancedConfig.DEFAULT_DURATION) }}</div>
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12">
              <el-form-item>
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
                  :value-on-clear="-1"
                  style="width: 100%"
                />
                <div class="cfg-unit">
                  {{
                    advancedConfig.MAX_LOCK_DURATION === -1
                      ? $t('botCreate.unlimited')
                      : formatSeconds(advancedConfig.MAX_LOCK_DURATION)
                  }}
                </div>
                <el-alert
                  v-if="advancedConfig.MAX_LOCK_DURATION > 86400"
                  :title="$t('botCreate.maxLockDurationWarning')"
                  type="warning"
                  :closable="false"
                  show-icon
                  style="margin-top: 6px"
                />
                <el-alert
                  v-if="
                    advancedConfig.MAX_LOCK_DURATION > 0 &&
                    advancedConfig.DEFAULT_DURATION > advancedConfig.MAX_LOCK_DURATION
                  "
                  :title="$t('botCreate.defaultExceedsMax')"
                  type="error"
                  :closable="false"
                  show-icon
                  style="margin-top: 6px"
                />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12">
              <el-form-item>
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
                  :value-on-clear="300"
                  style="width: 100%"
                />
                <div class="cfg-unit">{{ formatSeconds(advancedConfig.TIME_ALERT) }}</div>
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
            <el-col :xs="24" :sm="12">
              <el-form-item :label="$t('botCreate.forbidRelock')">
                <el-switch
                  v-model="advancedConfig.FORBID_RELOCK"
                  :aria-label="$t('botCreate.forbidRelock')"
                />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12">
              <el-form-item
                :label="
                  $t(
                    ['DEVICE', 'QUEUE_DEVICE'].includes(form.bot_type)
                      ? 'botCreate.maxUserCards'
                      : 'botCreate.maxUserNodes'
                  )
                "
              >
                <div class="resource-limit-control">
                  <el-checkbox v-model="resourceCountUnlimited">{{
                    $t('botCreate.unlimitedResources')
                  }}</el-checkbox>
                  <el-input-number
                    v-if="!resourceCountUnlimited"
                    v-model="advancedConfig.MAX_RESOURCES_PER_USER"
                    :min="1"
                    :step="1"
                    :value-on-clear="1"
                    :aria-label="$t('botCreate.maxUserResources')"
                  />
                </div>
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
            <el-col :xs="24">
              <el-form-item>
                <template #label>
                  {{ $t('botCreate.durationWhitelist') }}
                  <el-tooltip
                    :content="$t('botCreate.durationWhitelistHelp')"
                    placement="top"
                    effect="light"
                  >
                    <el-icon class="help-icon"><QuestionFilled /></el-icon>
                  </el-tooltip>
                </template>
                <el-select
                  v-model="advancedConfig.DURATION_WHITELIST"
                  multiple
                  filterable
                  allow-create
                  default-first-option
                  :reserve-keyword="false"
                  :placeholder="$t('botCreate.durationWhitelistPlaceholder')"
                  style="width: 100%"
                />
              </el-form-item>
            </el-col>
            <el-col :xs="24">
              <el-form-item>
                <template #label>
                  {{ $t('botCreate.showNodeAlias') }}
                  <el-tooltip
                    :content="$t('botCreate.showNodeAliasHelp')"
                    placement="top"
                    effect="light"
                  >
                    <el-icon class="help-icon"><QuestionFilled /></el-icon>
                  </el-tooltip>
                </template>
                <el-switch v-model="advancedConfig.SHOW_NODE_ALIAS" />
              </el-form-item>
            </el-col>
            <el-col :xs="24">
              <el-form-item>
                <template #label>
                  {{ $t('botCreate.allowUserAlias') }}
                  <el-tooltip
                    :content="$t('botCreate.allowUserAliasHelp')"
                    placement="top"
                    effect="light"
                  >
                    <el-icon class="help-icon"><QuestionFilled /></el-icon>
                  </el-tooltip>
                </template>
                <el-switch v-model="advancedConfig.ALLOW_USER_ALIAS" />
              </el-form-item>
            </el-col>
            <el-col v-if="['DEVICE', 'QUEUE_DEVICE'].includes(form.bot_type)" :xs="24">
              <el-form-item>
                <template #label>
                  {{ $t('botCreate.displayDeviceModel') }}
                  <el-tooltip
                    :content="$t('botCreate.displayDeviceModelHelp')"
                    placement="top"
                    effect="light"
                  >
                    <el-icon class="help-icon"><QuestionFilled /></el-icon>
                  </el-tooltip>
                </template>
                <el-switch v-model="advancedConfig.DISPLAY_DEVICE_MODEL" />
              </el-form-item>
            </el-col>
          </el-row>
        </el-collapse-item>
      </el-collapse>

      <!-- Actions -->
      <div class="form-actions">
        <el-button @click="$router.back()">{{ $t('common.cancel') }}</el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="(isEdit && !bot) || (!isEdit && !platformAccess[form.platform])"
          @click="handleSubmit"
        >
          {{ isEdit ? $t('common.save') : $t('common.create') }}
        </el-button>
      </div>
    </el-form>
  </div>
</template>

<script setup>
import { copyBotTemplate } from '../utils/botTemplate'
import FeishuGroupInfo from '../components/FeishuGroupInfo.vue'
import { ref, reactive, computed, onMounted, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { ArrowLeft, QuestionFilled } from '@element-plus/icons-vue'
import api from '../utils/api'
import { changedConfig } from '../utils/botSave'
import { useBotsStore } from '../stores/bots'
import { useAuthStore } from '../stores/auth'
import NodeBotForm from '../components/BotForm/NodeBotForm.vue'
import DeviceBotForm from '../components/BotForm/DeviceBotForm.vue'
import QueueBotForm from '../components/BotForm/QueueBotForm.vue'
import ClusterPreview from '../components/BotForm/ClusterPreview.vue'

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const botsStore = useBotsStore()
const auth = useAuthStore()
const platformAccess = reactive({ Infoflow: false, Feishu: false })
const feishuApplications = ref([])
async function loadFeishuApplications(visible) {
  if (visible && auth.isSuperAdmin)
    feishuApplications.value = (await api.get('/feishu/applications')).data
}

const isEdit = computed(() => !!route.params.id)
const formRef = ref()
const deviceFormRef = ref()
const saving = ref(false)
const clusterHasError = ref(false)
const bot = ref(null)
const agentAccess = ref(true)
const agentNotify = ref(true)
const editBaseline = ref(null)
const maskedAesKey = ref('')

watch(locale, () => {
  formRef.value?.clearValidate()
})
const maskedToken = ref('')
const nodeClusterConfig = ref({})
const deviceClusterConfig = ref({})
const clusterConfig = computed({
  get: () =>
    ['DEVICE', 'QUEUE_DEVICE'].includes(form.bot_type)
      ? deviceClusterConfig.value
      : nodeClusterConfig.value,
  set: (v) => {
    if (['DEVICE', 'QUEUE_DEVICE'].includes(form.bot_type)) deviceClusterConfig.value = v
    else nodeClusterConfig.value = v
  },
})

const form = reactive({
  name: '',
  bot_type: 'NODE',
  platform: 'Infoflow',
  feishu_application_id: null,
  group_id: '',
  webhook_url: '',
  aes_key: '',
  token: '',
})

// Advanced runtime config — maps to Bot.config_overrides
const advancedConfig = reactive({
  DEFAULT_DURATION: 7200,
  MAX_LOCK_DURATION: -1,
  FORBID_RELOCK: false,
  MAX_RESOURCES_PER_USER: -1,
  TIME_ALERT: 300,
  EARLY_NOTIFY: false,
  LANGUAGE: 'zh',
  DURATION_WHITELIST: [],
  NODE_ALIASES: {},
  SHOW_NODE_ALIAS: false,
  ALLOW_USER_ALIAS: false,
  DISPLAY_DEVICE_MODEL: false,
})

const resourceCountUnlimited = computed({
  get: () => advancedConfig.MAX_RESOURCES_PER_USER === -1,
  set: (value) => {
    advancedConfig.MAX_RESOURCES_PER_USER = value ? -1 : 1
  },
})

function formatSeconds(s) {
  if (s == null || s < 0) return ''
  if (s >= 3600 && s % 3600 === 0) return `= ${s / 3600}${t('botCreate.hours')}`
  if (s >= 3600) return `= ${(s / 3600).toFixed(1)}${t('botCreate.hours')}`
  if (s >= 60) return `= ${Math.round(s / 60)}${t('botCreate.minutes')}`
  return `= ${s}${t('botCreate.seconds')}`
}

const rules = computed(() => ({
  name: [{ required: true, message: () => t('botCreate.nameRequired'), trigger: 'blur' }],
  bot_type: [{ required: true, message: () => t('botCreate.typeRequired'), trigger: 'change' }],
  webhook_url: [
    {
      required: form.platform.toLowerCase() !== 'feishu',
      message: () => t('botCreate.webhookRequired'),
      trigger: 'blur',
    },
    { type: 'url', message: () => t('botCreate.webhookInvalid'), trigger: 'blur' },
  ],
  aes_key: [
    {
      required: !isEdit.value && form.platform.toLowerCase() !== 'feishu',
      message: () => t('botCreate.aesKeyRequired'),
      trigger: 'blur',
    },
  ],
  token: [
    {
      required: !isEdit.value && form.platform.toLowerCase() !== 'feishu',
      message: () => t('botCreate.tokenRequired'),
      trigger: 'blur',
    },
  ],
}))

onMounted(async () => {
  try {
    const { data } = await api.get('/settings')
    const settings = Object.fromEntries(data.map(({ key, value }) => [key, value]))
    platformAccess.Infoflow = settings.platform_infoflow_enabled !== 'false'
    platformAccess.Feishu = settings.platform_feishu_enabled !== 'false'
    if (!isEdit.value && !platformAccess.Infoflow && platformAccess.Feishu && auth.isSuperAdmin)
      form.platform = 'Feishu'
  } catch {
    ElMessage.error(t('common.error'))
  }

  if (isEdit.value || route.query.copy_from) {
    try {
      bot.value = await botsStore.getBot(isEdit.value ? route.params.id : route.query.copy_from)
      if (!isEdit.value) bot.value = copyBotTemplate(bot.value)
      agentAccess.value = bot.value.resource_auth_enabled === true
      agentNotify.value = bot.value.resource_api_notify_enabled !== false
      form.name = bot.value.name
      form.bot_type = bot.value.bot_type
      form.platform = bot.value.platform
      form.group_id = bot.value.group_id || ''
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
        advancedConfig.FORBID_RELOCK = overrides.FORBID_RELOCK ?? false
        advancedConfig.MAX_RESOURCES_PER_USER = overrides.MAX_RESOURCES_PER_USER ?? -1
        if (overrides.EARLY_NOTIFY != null) advancedConfig.EARLY_NOTIFY = overrides.EARLY_NOTIFY
        if (overrides.LANGUAGE != null) advancedConfig.LANGUAGE = overrides.LANGUAGE
        if (Array.isArray(overrides.DURATION_WHITELIST))
          advancedConfig.DURATION_WHITELIST = overrides.DURATION_WHITELIST
        if (overrides.NODE_ALIASES != null && typeof overrides.NODE_ALIASES === 'object')
          advancedConfig.NODE_ALIASES = overrides.NODE_ALIASES
        if (overrides.SHOW_NODE_ALIAS != null)
          advancedConfig.SHOW_NODE_ALIAS = overrides.SHOW_NODE_ALIAS
        if (overrides.ALLOW_USER_ALIAS != null)
          advancedConfig.ALLOW_USER_ALIAS = overrides.ALLOW_USER_ALIAS
        if (overrides.DISPLAY_DEVICE_MODEL != null)
          advancedConfig.DISPLAY_DEVICE_MODEL = overrides.DISPLAY_DEVICE_MODEL
      } catch {
        // keep defaults
      }
      try {
        const parsed =
          typeof bot.value.cluster_configs === 'string'
            ? JSON.parse(bot.value.cluster_configs)
            : bot.value.cluster_configs || {}
        if (['DEVICE', 'QUEUE_DEVICE'].includes(form.bot_type)) deviceClusterConfig.value = parsed
        else nodeClusterConfig.value = parsed
      } catch {
        if (['DEVICE', 'QUEUE_DEVICE'].includes(form.bot_type)) deviceClusterConfig.value = {}
        else nodeClusterConfig.value = {}
      }
      if (!isEdit.value) {
        form.name = `${bot.value.name} ${t('feishu.copySuffix')}`.slice(0, 64)
        form.group_id = ''
        form.webhook_url = ''
        form.aes_key = ''
        form.token = ''
        form.feishu_application_id = null
        maskedAesKey.value = ''
        maskedToken.value = ''
        if (!auth.isSuperAdmin || !platformAccess[form.platform])
          form.platform = platformAccess.Infoflow ? 'Infoflow' : 'Feishu'
      }
      await nextTick()
      editBaseline.value = JSON.parse(JSON.stringify(configPayload()))
    } catch {
      ElMessage.error(t('botForm.notFound'))
      router.replace('/bots')
    }
  }
})

function configPayload() {
  const data = { name: form.name, webhook_url: form.webhook_url }
  if (form.aes_key) data.aes_key = form.aes_key
  if (form.token) data.token = form.token
  const overrides = { ...advancedConfig }
  if (overrides.NODE_ALIASES && Object.keys(overrides.NODE_ALIASES).length === 0)
    delete overrides.NODE_ALIASES
  data.config_overrides = overrides
  const cc = clusterConfig.value
  if (cc && (Array.isArray(cc) ? cc.length > 0 : Object.keys(cc).length > 0))
    data.cluster_configs = cc
  return data
}
const configChanges = computed(() => changedConfig(configPayload(), editBaseline.value))
const configChanged = computed(() => Object.keys(configChanges.value).length > 0)

async function handleSubmit() {
  try {
    await formRef.value.validate()
  } catch (fields) {
    const errors = Object.values(fields || {}).flat()
    const msg = errors[0]?.message
    if (msg) ElMessage.error(typeof msg === 'function' ? msg() : msg)
    return
  }

  if (['DEVICE', 'QUEUE_DEVICE'].includes(form.bot_type) && deviceFormRef.value) {
    if (!deviceFormRef.value.validate()) {
      ElMessage.error(t('botForm.emptyNodeOrDevice'))
      return
    }
  }

  if (clusterHasError.value) {
    ElMessage.error(t('botForm.duplicateNode'))
    return
  }

  if (!isEdit.value) {
    const cc = clusterConfig.value
    const nodeCount = Array.isArray(cc) ? cc.length : cc ? Object.keys(cc).length : 0
    if (nodeCount === 0) {
      ElMessage.error(t('botCreate.clusterRequired'))
      return
    }
    if (['DEVICE', 'QUEUE_DEVICE'].includes(form.bot_type)) {
      const totalDevs = Array.isArray(cc)
        ? cc.reduce((sum, item) => sum + (item.devices?.length || 0), 0)
        : Object.values(cc).flat().length
      if (totalDevs === 0) {
        ElMessage.error(t('botForm.deviceRequired'))
        return
      }
    }
  } else {
    const cc = clusterConfig.value
    const nodeCount = Array.isArray(cc) ? cc.length : cc ? Object.keys(cc).length : 0
    if (nodeCount > 0 && ['DEVICE', 'QUEUE_DEVICE'].includes(form.bot_type)) {
      const totalDevs = Array.isArray(cc)
        ? cc.reduce((sum, item) => sum + (item.devices?.length || 0), 0)
        : Object.values(cc).flat().length
      if (totalDevs === 0) {
        ElMessage.error(t('botForm.deviceRequired'))
        return
      }
    }
  }

  saving.value = true
  try {
    if (isEdit.value) {
      const data = { ...configChanges.value }
      if (agentAccess.value !== bot.value.resource_auth_enabled)
        data.resource_auth_enabled = agentAccess.value
      if (agentNotify.value !== bot.value.resource_api_notify_enabled)
        data.resource_api_notify_enabled = agentNotify.value
      const needRestart = configChanged.value && bot.value.status !== 'stopped'
      if (Object.keys(data).length) await botsStore.updateBot(bot.value.id, data)
      ElMessage.success(t('common.success'))
      if (needRestart) {
        await api.post(`/bots/${bot.value.id}/stop`, null, { _silent: true }).catch(() => {})
        await api.post(`/bots/${bot.value.id}/start`, null, { _silent: true }).catch(() => {})
      }
      router.push(`/bots/${bot.value.id}`)
    } else {
      const created = await botsStore.createBot({
        ...form,
        ...configPayload(),
        cluster_configs: clusterConfig.value,
        resource_auth_enabled: agentAccess.value,
        resource_api_notify_enabled: agentNotify.value,
      })
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
.agent-settings {
  margin: 24px 0;
  padding: 0;
}
.agent-settings-title {
  margin-bottom: 8px;
  color: var(--lb-text-secondary);
  font-size: 12px;
  font-weight: 600;
}
.agent-setting-row {
  display: grid;
  grid-template-columns: minmax(0, 160px) 40px;
  align-items: center;
  justify-content: start;
  min-height: 40px;
  gap: 16px;
}
.agent-setting-label {
  display: inline-flex;
  align-items: center;
  font-size: 14px;
  color: var(--el-text-color-primary);
}
.agent-notify-row.is-disabled .agent-setting-label {
  color: var(--el-text-color-placeholder);
}
.resource-limit-control {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
  min-width: 0;
}
.resource-limit-control :deep(.el-checkbox) {
  margin-right: 0;
}
.resource-limit-control :deep(.el-input-number) {
  width: 148px;
  max-width: 100%;
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
