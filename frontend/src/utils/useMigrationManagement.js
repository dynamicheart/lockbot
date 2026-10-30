import { computed, onBeforeUnmount, ref, watch } from 'vue'
import api from './api'
import { createBotDetailScope } from './botDetailScope'

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
const actions = new Set([
  'source_challenge',
  'target_challenge',
  'wait_metrics',
  'resume',
  'completed',
  'aborted',
])
const phaseActions = {
  planned: ['source_challenge', 'resume'],
  freezing: ['resume'],
  checkpointed: ['wait_metrics', 'source_challenge', 'resume'],
  switched: ['target_challenge', 'resume'],
  activating: ['target_challenge', 'resume'],
  aborting: ['resume'],
  completed: ['completed'],
  aborted: ['aborted'],
}
const hex = (value, size) =>
  typeof value === 'string' && new RegExp(`^[a-f0-9]{${size}}$`).test(value)
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
    !phaseActions[data.phase]?.includes(data.next_action) ||
    data.commands_replayed !== false ||
    data.active_state_migration_supported !== false ||
    !Number.isInteger(data.target_application_id) ||
    data.target_application_id <= 0 ||
    !/^oc_[A-Za-z0-9_-]{1,125}$/.test(data.target_chat_id || '') ||
    data.target_revision !== data.source_revision + 1 ||
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
    reviewedTarget = ref(false)
  const publicationRef = ref(''),
    publicationReviewed = ref(false),
    challenge = ref(null)
  const busy = ref(false),
    error = ref(''),
    denied = ref(false),
    pendingId = ref('')
  const needsRefresh = ref(false)
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
      applications.value.some((item) => item.id === applicationId.value) &&
      /^oc_[A-Za-z0-9_-]{1,125}$/.test(chatId.value.trim())
  )
  const canChallenge = computed(
    () =>
      enabled.value &&
      !busy.value &&
      operation.value &&
      ['source_challenge', 'target_challenge'].includes(operation.value.next_action) &&
      !needsRefresh.value &&
      hex(publicationRef.value.trim(), 32) &&
      publicationReviewed.value
  )

  function failure(err) {
    busy.value = false
    const status = err.response?.status
    if (status === 401 || status === 403) {
      scope.enter('')
      denied.value = true
      operation.value = capability.value = challenge.value = null
      applications.value = []
      publicationRef.value = pendingId.value = ''
      publicationReviewed.value = reviewedTarget.value = false
      error.value = status === 401 ? 'auth.sessionExpired' : 'common.permissionDenied'
    } else if (
      status === 503 &&
      err.response?.data?.detail?.code === 'migration_execution_disabled'
    ) {
      capability.value = { enabled: false }
      challenge.value = null
      error.value = 'migrationManagement.disabled'
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
        value.target_chat_id !== operation.value.target_chat_id)
    )
      throw new Error('Mixed target')
    return value
  }
  async function load() {
    if (!props.authorized) return
    const request = scope.begin('load')
    if (!request) return
    busy.value = true
    error.value = ''
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
        typeof data.supported_source !== 'boolean' ||
        data.empty_state_only !== true
      )
        throw new Error('Invalid capability')
      capability.value = { enabled: data.enabled, supported_source: data.supported_source }
      operation.value = data.latest_operation ? project(data.latest_operation) : null
      needsRefresh.value = false
      pendingId.value = operation.value?.operation_id || ''
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
    challenge.value = null
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
    if (kind === 'target-challenge' && !canChallenge.value) return
    if (kind === 'resume' && operation.value?.can_resume !== true) return
    if (kind === 'abort' && operation.value?.can_abort !== true) return
    if (!['plan', 'target-challenge', 'resume', 'abort'].includes(kind)) return
    const request = scope.begin('action')
    if (!request) return
    const target = { id: applicationId.value, chat: chatId.value.trim() }
    const before = operation.value
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
        ? { target_application_id: target.id, target_chat_id: target.chat, operation_id: id }
        : kind === 'target-challenge'
          ? {
              reviewed_target_digest: before.target_digest,
              publication_evidence_ref: publicationRef.value.trim(),
              publication_reviewed: true,
            }
          : { expected_phase: before.phase }
    busy.value = true
    error.value = ''
    challenge.value = null
    try {
      const { data } = await api.post(path, body, { signal: request.signal, _silent: true })
      if (!request.isCurrent()) return
      const status = project(kind === 'target-challenge' ? data.status : data, id)
      if (
        kind === 'plan' &&
        (status.target_application_id !== target.id || status.target_chat_id !== target.chat)
      )
        throw new Error('Mixed plan')
      if (kind === 'target-challenge') {
        if (
          data.can_commit !== false ||
          !/^migration-check [A-Za-z0-9_-]{43}$/.test(data.command || '') ||
          !Number.isFinite(Date.parse(data.expires_at)) ||
          Date.parse(data.expires_at) <= Date.now() ||
          Date.parse(data.expires_at) > Date.now() + 11 * 60 * 1000
        )
          throw new Error('Invalid challenge')
        challenge.value = { command: data.command, expires_at: data.expires_at }
      }
      operation.value = status
      publicationReviewed.value = false
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
      reviewedTarget.value = false
      operation.value = challenge.value = null
      pendingId.value = ''
      error.value = ''
      publicationRef.value = ''
      publicationReviewed.value = false
    },
    { flush: 'sync' }
  )
  watch(
    () => [props.botId, props.botUuid, props.identityRevision, props.authorized, props.authScope],
    () => {
      scope.enter(props.authorized && props.botUuid ? props.botId : '')
      applicationId.value = null
      chatId.value = ''
      reviewedTarget.value = false
      operation.value = challenge.value = capability.value = null
      applications.value = []
      publicationRef.value = pendingId.value = error.value = ''
      publicationReviewed.value = busy.value = denied.value = needsRefresh.value = false
      if (props.authorized && props.botUuid) load()
    },
    { immediate: true, flush: 'sync' }
  )
  let expiryTimer
  watch(challenge, (value) => {
    clearTimeout(expiryTimer)
    if (value)
      expiryTimer = setTimeout(
        () => {
          challenge.value = null
        },
        Math.max(0, Date.parse(value.expires_at) - Date.now())
      )
  })
  onBeforeUnmount(() => {
    clearTimeout(expiryTimer)
    scope.dispose()
  })
  return {
    capability,
    applications,
    operation,
    applicationId,
    chatId,
    reviewedTarget,
    publicationRef,
    publicationReviewed,
    challenge,
    busy,
    error,
    denied,
    pendingId,
    enabled,
    canPlan,
    canChallenge,
    needsRefresh,
    load,
    inspect,
    mutate,
  }
}
