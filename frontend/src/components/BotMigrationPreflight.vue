<template>
  <el-dialog
    :model-value="true"
    :title="$t('migration.title')"
    width="640px"
    top="24px"
    class="migration-dialog"
    @close="$emit('close')"
  >
    <div class="migration-heading">
      <span>{{ $t('migration.readOnly') }}</span>
      <el-tooltip :content="$t('migration.help')" placement="top">
        <el-icon class="help-icon"><QuestionFilled /></el-icon>
      </el-tooltip>
    </div>
    <el-form label-position="top" @submit.prevent="check">
      <el-form-item :label="$t('feishu.application')">
        <el-select
          v-model="applicationId"
          :loading="applicationsLoading"
          :disabled="denied || !authorized"
          :placeholder="$t('migration.selectApplication')"
        >
          <el-option
            v-for="item in applications"
            :key="item.id"
            :label="item.name"
            :value="item.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item>
        <template #label>
          {{ $t('feishu.group') }}
          <el-tooltip :content="$t('migration.chatHelp')" placement="top">
            <el-icon class="help-icon"><QuestionFilled /></el-icon>
          </el-tooltip>
        </template>
        <el-input v-model="chatId" placeholder="oc_…" :disabled="denied || !authorized" />
      </el-form-item>
      <el-button type="primary" :disabled="!canCheck" :loading="checking" @click="check">
        {{ $t('migration.check') }}
      </el-button>
    </el-form>
    <p v-if="error" class="migration-error" role="alert">{{ $t(error) }}</p>
    <p v-else-if="!applicationsLoading && !applications.length" class="migration-note">
      {{ $t('migration.noApplications') }}
    </p>
    <BotMigrationPreflightResult v-if="result" :result="result" />
    <template #footer>
      <el-button @click="$emit('close')">{{ $t('common.close') }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { QuestionFilled } from '@element-plus/icons-vue'
import BotMigrationPreflightResult from './BotMigrationPreflightResult.vue'
import api from '../utils/api'
import { createBotDetailScope } from '../utils/botDetailScope'

const props = defineProps({
  botId: { type: [Number, String], required: true },
  authorized: { type: Boolean, default: false },
  identityRevision: { type: Number, default: 0 },
})
defineEmits(['close'])
const scope = createBotDetailScope()
const applications = ref([])
const applicationsLoading = ref(false)
const applicationId = ref(null)
const chatId = ref('')
const checking = ref(false)
const denied = ref(false)
const result = ref(null)
const error = ref('')
const countFields = ['resources', 'holders', 'reservations', 'offers', 'non_idle']
const blockerKeys = Object.fromEntries(
  [
    'source_identity_rules_unrecognized',
    'source_identity_rules_require_mapping',
    'source_generation_unhealthy',
    'source_generation_changed',
    'source_inventory_mismatch',
    'source_state_unrecognized',
    'source_state_unavailable',
    'source_status_not_running',
    'source_platform_unsupported',
    'source_type_unsupported',
    'source_identity_missing',
    'source_has_outstanding_allocations',
    'target_application_missing',
    'target_application_identity_unknown',
    'target_membership_unverified',
    'target_membership_identity_changed',
    'target_group_already_bound',
    'source_already_has_feishu_route',
    'cutover_not_implemented',
  ].map((key) => [key, `migration.blockers.${key}`])
)
const unverifiedKeys = Object.fromEntries(
  [
    'application_publication',
    'application_credentials',
    'receiver_connectivity',
    'current_group_membership',
    'durable_freeze_and_recovery',
  ].map((key) => [key, `migration.unverified.${key}`])
)
const canCheck = computed(
  () =>
    props.authorized &&
    !denied.value &&
    !checking.value &&
    applications.value.some((item) => item.id === applicationId.value) &&
    /^oc_[A-Za-z0-9_-]+$/.test(chatId.value.trim()) &&
    chatId.value.trim().length <= 128
)

function failure(err) {
  const status = err.response?.status
  if (status === 401 || status === 403) {
    denied.value = true
    applications.value = []
    applicationId.value = null
    error.value = status === 401 ? 'auth.sessionExpired' : 'common.permissionDenied'
  } else {
    error.value = status === 404 ? 'migration.notAvailable' : 'common.requestFailed'
  }
}
async function loadApplications() {
  const request = scope.begin('applications')
  if (!request) return
  applicationsLoading.value = true
  try {
    const { data } = await api.get('/feishu/applications', {
      signal: request.signal,
      _silent: true,
    })
    if (request.isCurrent())
      applications.value = Array.isArray(data)
        ? data
            .filter(
              (item) => Number.isInteger(item.id) && item.id > 0 && typeof item.name === 'string'
            )
            .map(({ id, name }) => ({ id, name }))
        : []
  } catch (err) {
    if (request.isCurrent()) failure(err)
  } finally {
    if (request.isCurrent()) applicationsLoading.value = false
    request.finish()
  }
}
async function check() {
  if (!canCheck.value) return
  const request = scope.begin('preflight')
  if (!request) return
  const target = { application_id: applicationId.value, chat_id: chatId.value.trim() }
  result.value = null
  error.value = ''
  checking.value = true
  try {
    const { data } = await api.post(`/bots/${request.id}/migration-preflight`, target, {
      signal: request.signal,
      _silent: true,
    })
    if (!request.isCurrent()) return
    if (
      data?.schema_version !== 1 ||
      data.read_only !== true ||
      data.can_commit !== false ||
      String(data.source?.bot_id) !== request.id ||
      data.source?.identity_revision !== props.identityRevision ||
      data.target?.application_id !== target.application_id ||
      data.target?.chat_id !== target.chat_id ||
      !Array.isArray(data.blockers) ||
      !Array.isArray(data.unverified)
    ) {
      error.value = 'migration.invalidResponse'
      return
    }
    // Only retain the fields this read-only panel renders, never a raw response dump.
    result.value = {
      stored_obligations: data.stored_obligations
        ? Object.fromEntries(
            countFields.map((field) => [
              field,
              Number.isInteger(data.stored_obligations[field]) &&
              data.stored_obligations[field] >= 0
                ? data.stored_obligations[field]
                : null,
            ])
          )
        : null,
      blockers: data.blockers.map((item) => (Object.hasOwn(blockerKeys, item) ? item : 'unknown')),
      unverified: data.unverified.map((item) =>
        Object.hasOwn(unverifiedKeys, item) ? item : 'unknown'
      ),
    }
  } catch (err) {
    if (request.isCurrent()) failure(err)
  } finally {
    if (request.isCurrent()) checking.value = false
    request.finish()
  }
}
watch(
  [applicationId, chatId],
  () => {
    scope.cancel('preflight')
    result.value = null
    checking.value = false
    error.value = ''
  },
  { flush: 'sync' }
)
watch(
  () => [props.botId, props.authorized, props.identityRevision],
  () => {
    scope.enter(props.authorized ? props.botId : '')
    applicationId.value = null
    chatId.value = ''
    applications.value = []
    result.value = null
    error.value = ''
    denied.value = false
    applicationsLoading.value = checking.value = false
    if (props.authorized) loadApplications()
  },
  { immediate: true, flush: 'sync' }
)
onBeforeUnmount(() => scope.dispose())
</script>

<style scoped>
:global(.migration-dialog.el-dialog) {
  display: flex;
  flex-direction: column;
  max-width: calc(100vw - 32px);
  max-height: calc(100dvh - 48px);
  margin-bottom: 24px;
}
:global(.migration-dialog .el-dialog__body) {
  min-height: 0;
  overflow-y: auto;
}
:global(.migration-dialog .el-dialog__header),
:global(.migration-dialog .el-dialog__footer) {
  flex-shrink: 0;
}
.migration-heading {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 18px;
  color: var(--el-text-color-secondary);
}
.help-icon {
  color: var(--el-text-color-secondary);
  cursor: help;
  font-size: 13px;
}
.el-select {
  width: 100%;
}
.migration-error {
  color: var(--el-color-danger);
}
</style>
