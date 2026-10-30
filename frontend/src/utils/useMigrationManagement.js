import { computed, onBeforeUnmount, ref, watch } from 'vue'
import api from './api'
import { createBotDetailScope } from './botDetailScope'

export const resetCountKeys = [
  'holders',
  'reservations',
  'offers',
  'agent_bindings',
  'declarations',
  'op_keys',
  'identity_whitelist_users',
]
const phases = new Set([
  'planned',
  'freezing',
  'checkpointed',
  'switched',
  'activating',
  'aborting',
  'completed',
  'aborted',
])
const actions = new Set(['wait_metrics', 'resume', 'completed', 'aborted'])
const hex = (value, size) =>
  typeof value === 'string' && new RegExp(`^[a-f0-9]{${size}}$`).test(value)
function counts(data) {
  if (!data || resetCountKeys.some((key) => !Number.isSafeInteger(data[key]) || data[key] < 0))
    throw new Error('Invalid reset counts')
  return Object.fromEntries(resetCountKeys.map((key) => [key, data[key]]))
}
export function migrationStatus(data, botId, revision) {
  if (
    !data ||
    String(data.bot_id) !== String(botId) ||
    !Number.isInteger(data.source_revision) ||
    (data.source_revision !== revision &&
      !(
        ['switched', 'activating', 'completed'].includes(data.phase) &&
        data.target_revision === revision
      )) ||
    !hex(data.operation_id, 32) ||
    !hex(data.target_digest, 64) ||
    !phases.has(data.phase) ||
    !actions.has(data.next_action) ||
    data.commands_replayed !== false ||
    data.active_state_migration_supported !== false ||
    !Number.isInteger(data.target_application_id) ||
    data.target_application_id <= 0 ||
    !/^oc_[A-Za-z0-9_-]{1,125}$/.test(data.target_chat_id || '') ||
    data.target_revision !== data.source_revision + 1 ||
    typeof data.reset_applied !== 'boolean' ||
    (data.can_resume === true && data.next_action !== 'resume') ||
    (data.can_abort === true &&
      !['planned', 'freezing', 'checkpointed', 'aborting'].includes(data.phase))
  )
    throw new Error('Invalid migration status')
  return {
    operation_id: data.operation_id,
    phase: data.phase,
    target_digest: data.target_digest,
    target_application_id: data.target_application_id,
    target_application_name:
      typeof data.target_application_name === 'string'
        ? data.target_application_name.slice(0, 160)
        : '',
    target_chat_id: data.target_chat_id,
    next_action: data.next_action,
    source_revision: data.source_revision,
    target_revision: data.target_revision,
    can_resume: data.can_resume === true,
    can_abort: data.can_abort === true,
    reset_counts: counts(data.reset_counts),
    reset_applied: data.reset_applied,
    metrics_not_before:
      typeof data.metrics_not_before === 'string' &&
      Number.isFinite(Date.parse(data.metrics_not_before))
        ? data.metrics_not_before
        : null,
  }
}

export function useMigrationManagement(props) {
  const scope = createBotDetailScope()
  const capability = ref(null),
    applications = ref([]),
    operation = ref(null)
  const applicationId = ref(null),
    chatId = ref(''),
    reviewedTarget = ref(false),
    clearConfirmed = ref(false)
  const busy = ref(false),
    error = ref(''),
    denied = ref(false),
    pendingId = ref('')
  const needsRefresh = ref(false),
    stalePlan = ref(false)
  const enabled = computed(
    () => props.authorized && !denied.value && capability.value?.enabled === true
  )
  const canPlan = computed(
    () =>
      enabled.value &&
      capability.value?.supported_source === true &&
      !busy.value &&
      !operation.value &&
      !pendingId.value &&
      !needsRefresh.value &&
      reviewedTarget.value &&
      clearConfirmed.value &&
      capability.value.reset_preview &&
      applications.value.some((item) => item.id === applicationId.value) &&
      /^oc_[A-Za-z0-9_-]{1,125}$/.test(chatId.value.trim())
  )
  function failure(err) {
    busy.value = false
    const status = err.response?.status,
      code = err.response?.data?.detail?.code
    if (status === 401 || status === 403) {
      scope.enter('')
      denied.value = true
      operation.value = capability.value = null
      applications.value = []
      pendingId.value = ''
      reviewedTarget.value = clearConfirmed.value = false
      error.value = status === 401 ? 'auth.sessionExpired' : 'common.permissionDenied'
    } else if (code === 'migration_execution_disabled') {
      capability.value = { enabled: false }
      error.value = 'migrationManagement.disabled'
    } else if (status === 409 && code === 'reset_preview_changed') {
      stalePlan.value = true
      error.value = 'migrationManagement.stateChanged'
    } else
      error.value =
        status === 404 ? 'migrationManagement.unavailable' : 'migrationManagement.unconfirmed'
  }
  function project(data, expectedId = '') {
    const value = migrationStatus(data, props.botId, props.identityRevision)
    if (expectedId && value.operation_id !== expectedId) throw new Error('Mixed operation')
    if (
      operation.value &&
      value.operation_id === operation.value.operation_id &&
      (value.target_digest !== operation.value.target_digest ||
        value.target_application_id !== operation.value.target_application_id ||
        value.target_chat_id !== operation.value.target_chat_id ||
        JSON.stringify(value.reset_counts) !== JSON.stringify(operation.value.reset_counts))
    )
      throw new Error('Mixed reset review')
    return value
  }
  async function load(newReview = false) {
    if (!props.authorized || busy.value) return
    if (newReview && operation.value?.phase !== 'aborted') return
    const request = scope.begin('load')
    if (!request) return
    busy.value = true
    error.value = ''
    reviewedTarget.value = clearConfirmed.value = false
    try {
      const { data } = await api.get(`/bots/${request.id}/migration-management`, {
        signal: request.signal,
        _silent: true,
      })
      if (!request.isCurrent()) return
      if (
        data.source_uuid !== props.botUuid ||
        data.identity_revision !== props.identityRevision ||
        typeof data.enabled !== 'boolean' ||
        typeof data.supported_source !== 'boolean'
      )
        throw new Error('Invalid capability')
      const preview = data.reset_preview
      if (data.enabled && data.supported_source && (!hex(preview?.digest, 64) || !preview?.counts))
        throw new Error('Invalid reset preview')
      capability.value = {
        enabled: data.enabled,
        supported_source: data.supported_source,
        reset_preview:
          preview && hex(preview.digest, 64)
            ? { digest: preview.digest, counts: counts(preview.counts) }
            : null,
      }
      const latest = data.latest_operation ? project(data.latest_operation) : null
      operation.value = newReview && latest?.phase === 'aborted' ? null : latest
      pendingId.value = operation.value?.operation_id || ''
      needsRefresh.value = stalePlan.value = false
      if (data.enabled && data.supported_source && !operation.value) {
        const response = await api.get('/feishu/applications', {
          signal: request.signal,
          _silent: true,
        })
        if (!request.isCurrent()) return
        applications.value = Array.isArray(response.data)
          ? response.data
              .filter(
                (item) => Number.isInteger(item.id) && item.id > 0 && typeof item.name === 'string'
              )
              .map(({ id, name }) => ({ id, name }))
          : []
      }
    } catch (err) {
      if (request.isCurrent()) failure(err)
    } finally {
      if (request.isCurrent()) busy.value = false
      request.finish()
    }
  }
  async function inspect() {
    if (!enabled.value || busy.value || !hex(pendingId.value, 32)) return
    const request = scope.begin('action')
    if (!request) return
    const id = pendingId.value
    busy.value = true
    error.value = ''
    try {
      const { data } = await api.get(`/bot-migrations/${id}`, {
        signal: request.signal,
        _silent: true,
      })
      if (request.isCurrent()) {
        operation.value = project(data, id)
        needsRefresh.value = false
      }
    } catch (err) {
      if (request.isCurrent()) failure(err)
    } finally {
      if (request.isCurrent()) busy.value = false
      request.finish()
    }
  }
  async function mutate(kind) {
    if (busy.value || !enabled.value || needsRefresh.value) return
    if (kind === 'plan' && !canPlan.value) return
    if (kind === 'resume' && (operation.value?.can_resume !== true || stalePlan.value)) return
    if (kind === 'abort' && operation.value?.can_abort !== true) return
    if (!['plan', 'resume', 'abort'].includes(kind)) return
    const request = scope.begin('action')
    if (!request) return
    const target = { id: applicationId.value, chat: chatId.value.trim() },
      before = operation.value
    const preview = capability.value.reset_preview
    if (kind === 'plan') {
      const bytes = new Uint8Array(16)
      globalThis.crypto.getRandomValues(bytes)
      pendingId.value = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
    }
    const id = pendingId.value
    const path =
      kind === 'plan' ? `/bots/${request.id}/migration-plan` : `/bot-migrations/${id}/${kind}`
    const body =
      kind === 'plan'
        ? {
            target_application_id: target.id,
            target_chat_id: target.chat,
            operation_id: id,
            reviewed_reset_digest: preview.digest,
            clear_live_state: true,
          }
        : { expected_phase: before.phase }
    busy.value = true
    error.value = ''
    try {
      const { data } = await api.post(path, body, { signal: request.signal, _silent: true })
      if (!request.isCurrent()) return
      const status = project(data, id)
      if (
        kind === 'plan' &&
        (status.target_application_id !== target.id ||
          status.target_chat_id !== target.chat ||
          status.reset_applied ||
          JSON.stringify(status.reset_counts) !== JSON.stringify(preview.counts))
      )
        throw new Error('Mixed reset plan')
      operation.value = status
      reviewedTarget.value = clearConfirmed.value = false
    } catch (err) {
      if (request.isCurrent()) {
        needsRefresh.value = true
        failure(err)
      }
    } finally {
      if (request.isCurrent()) busy.value = false
      request.finish()
    }
  }
  watch(
    [applicationId, chatId],
    () => {
      scope.cancel('action')
      busy.value = false
      reviewedTarget.value = clearConfirmed.value = false
      operation.value = null
      pendingId.value = ''
      error.value = ''
    },
    { flush: 'sync' }
  )
  watch(
    () => [props.botId, props.botUuid, props.identityRevision, props.authorized, props.authScope],
    () => {
      scope.enter(props.authorized && props.botUuid ? props.botId : '')
      applicationId.value = null
      chatId.value = ''
      operation.value = capability.value = null
      applications.value = []
      pendingId.value = error.value = ''
      reviewedTarget.value =
        clearConfirmed.value =
        busy.value =
        denied.value =
        needsRefresh.value =
        stalePlan.value =
          false
      if (props.authorized && props.botUuid) load()
    },
    { immediate: true, flush: 'sync' }
  )
  onBeforeUnmount(() => scope.dispose())
  return {
    capability,
    applications,
    operation,
    applicationId,
    chatId,
    reviewedTarget,
    clearConfirmed,
    busy,
    error,
    pendingId,
    enabled,
    canPlan,
    needsRefresh,
    stalePlan,
    load,
    inspect,
    mutate,
  }
}
