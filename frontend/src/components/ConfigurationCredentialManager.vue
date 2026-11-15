<template>
  <section ref="container" class="configuration-credentials">
    <header class="credential-toolbar">
      <p>{{ $t('configurationCredentials.description') }}</p>
      <div class="credential-actions">
        <el-button :loading="loading" @click="load(offset)">{{ $t('common.refresh') }}</el-button>
        <el-button type="primary" :disabled="!bots.length || busy" @click="openCreate">
          {{ $t('configurationCredentials.create') }}
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="error && !showCreate && !showAction"
      :title="$t(error)"
      type="error"
      :closable="false"
      show-icon
    />

    <el-table v-loading="loading" :data="items" :empty-text="$t('configurationCredentials.empty')">
      <el-table-column v-if="compact" type="expand" width="38">
        <template #default="{ row }">
          <dl class="credential-details">
            <dt>{{ $t('auditCredentials.bots') }}</dt>
            <dd>{{ botNames(row).join(', ') }}</dd>
            <dt>{{ $t('auditCredentials.expires') }}</dt>
            <dd>{{ formatDateTime(row.expires_at) }}</dd>
            <dt>{{ $t('configurationCredentials.lastUsed') }}</dt>
            <dd>{{ row.last_used_at ? formatDateTime(row.last_used_at) : '—' }}</dd>
          </dl>
        </template>
      </el-table-column>
      <el-table-column :label="$t('auditCredentials.name')" :min-width="compact ? 100 : 170">
        <template #default="{ row }">
          <span class="credential-name">{{ row.label }}</span>
          <div v-if="compact" class="credential-summary">
            {{ $t(`auditCredentials.${status(row)}`) }} · {{ scopeText(row) }}
          </div>
        </template>
      </el-table-column>
      <el-table-column
        v-if="!compact"
        :label="$t('auditCredentials.bots')"
        min-width="180"
        show-overflow-tooltip
      >
        <template #default="{ row }">
          {{ botNames(row).join(', ') }}
        </template>
      </el-table-column>
      <el-table-column v-if="!compact" :label="$t('auditCredentials.scope')" min-width="200">
        <template #default="{ row }">
          {{ scopeText(row) }}
        </template>
      </el-table-column>
      <el-table-column v-if="!compact" :label="$t('auditCredentials.expires')" width="165">
        <template #default="{ row }">{{ formatDateTime(row.expires_at) }}</template>
      </el-table-column>
      <el-table-column v-if="!compact" :label="$t('configurationCredentials.lastUsed')" width="165">
        <template #default="{ row }">{{
          row.last_used_at ? formatDateTime(row.last_used_at) : '—'
        }}</template>
      </el-table-column>
      <el-table-column v-if="!compact" :label="$t('auditCredentials.status')" width="100">
        <template #default="{ row }">{{ $t(`auditCredentials.${status(row)}`) }}</template>
      </el-table-column>
      <el-table-column :label="$t('common.actions')" :width="compact ? 122 : 145">
        <template #default="{ row }">
          <el-button
            v-if="status(row) === 'active'"
            link
            :disabled="busy"
            @click="openAction('rotate', row)"
          >
            {{ $t('configurationCredentials.rotate') }}
          </el-button>
          <el-button
            v-if="!row.revoked_at"
            link
            type="danger"
            :disabled="busy"
            @click="openAction('revoke', row)"
          >
            {{ $t('auditCredentials.revoke') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <div
      v-if="offset || items.length === pageSize"
      class="credential-actions credential-pagination"
    >
      <el-button :disabled="!offset || loading" @click="load(offset - pageSize)">
        {{ $t('auditCredentials.previous') }}
      </el-button>
      <el-button :disabled="items.length !== pageSize || loading" @click="load(offset + pageSize)">
        {{ $t('auditCredentials.next') }}
      </el-button>
    </div>

    <el-dialog
      v-model="showCreate"
      :title="$t('configurationCredentials.create')"
      width="min(520px, 92vw)"
    >
      <el-form label-position="top" @submit.prevent="create">
        <el-form-item :label="$t('auditCredentials.name')">
          <el-input v-model="form.label" maxlength="64" autocomplete="off" />
        </el-form-item>
        <el-form-item :label="$t('auditCredentials.bots')">
          <el-select
            v-model="form.bot_ids"
            :placeholder="$t('auditCredentials.selectBots')"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            :multiple-limit="100"
          >
            <el-option v-for="bot in bots" :key="bot.id" :value="bot.id" :label="botLabel(bot)" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('auditCredentials.scope')">
          <el-checkbox-group v-model="form.scopes">
            <el-checkbox
              v-for="(label, permission) in scopeLabels"
              :key="permission"
              :value="permission"
            >
              {{ $t(label) }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item :label="$t('auditCredentials.lifetime')">
          <el-select v-model="form.expires_in_seconds">
            <el-option
              v-for="seconds in lifetimes"
              :key="seconds"
              :value="seconds"
              :label="$t(`auditCredentials.ttl${seconds}`)"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <el-alert v-if="error" :title="$t(error)" type="error" :closable="false" show-icon />
      <template #footer>
        <el-button @click="showCreate = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :disabled="!canCreate" :loading="busy" @click="create">
          {{ $t('configurationCredentials.create') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showAction" :title="$t(actionLabel)" width="min(480px, 92vw)">
      <p v-if="action">
        {{ $t(`configurationCredentials.${action.type}Confirm`, { name: action.row.label }) }}
      </p>
      <el-form
        v-if="action?.type === 'rotate'"
        label-position="top"
        @submit.prevent="confirmAction"
      >
        <el-form-item :label="$t('auditCredentials.lifetime')">
          <el-select v-model="rotateLifetime">
            <el-option
              v-for="seconds in lifetimes"
              :key="seconds"
              :value="seconds"
              :label="$t(`auditCredentials.ttl${seconds}`)"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <el-alert v-if="error" :title="$t(error)" type="error" :closable="false" show-icon />
      <template #footer>
        <el-button @click="showAction = false">{{ $t('common.cancel') }}</el-button>
        <el-button
          :type="action?.type === 'revoke' ? 'danger' : 'primary'"
          :loading="busy"
          @click="confirmAction"
        >
          {{ $t(actionLabel) }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="showSecret"
      :title="$t('configurationCredentials.shownOnce')"
      width="min(560px, 92vw)"
      destroy-on-close
    >
      <p>{{ $t('configurationCredentials.saveHint') }}</p>
      <el-input
        :model-value="secret"
        :aria-label="$t('nav.apiCredentials')"
        readonly
        autocomplete="off"
      />
      <template #footer>
        <el-button :disabled="!secret" @click="copyText(secret)">{{ $t('common.copy') }}</el-button>
        <el-button type="primary" @click="showSecret = false">{{
          $t('auditCredentials.saved')
        }}</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
import { createBotDetailScope } from '../utils/botDetailScope'
import { useHelpers } from '../utils/helpers'

const props = defineProps({
  principalId: { type: [Number, String], default: '' },
  sessionRevision: { type: Number, default: 0 },
})
const { copyText, formatDateTime } = useHelpers()
const { t } = useI18n()
const scope = createBotDetailScope()
const endpoint = '/configuration-credentials'
const scopeLabels = {
  'node_access:write': 'configurationCredentials.nodeAccess',
  'datasource:write': 'configurationCredentials.datasource',
}
const lifetimes = [600, 3600, 86400, 604800]
const pageSize = 50
const defaultLifetime = 86400
const container = ref(null)
const compact = ref(false)
const items = ref([])
const bots = ref([])
const offset = ref(0)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const showCreate = ref(false)
const showAction = ref(false)
const action = ref(null)
const rotateLifetime = ref(defaultLifetime)
const secret = ref('')
const form = ref(emptyForm())
const now = ref(Date.now())
const canCreate = computed(
  () => form.value.label.trim() && form.value.bot_ids.length && form.value.scopes.length
)
const actionLabel = computed(() =>
  action.value?.type === 'rotate' ? 'configurationCredentials.rotate' : 'auditCredentials.revoke'
)
const showSecret = computed({
  get: () => Boolean(secret.value),
  set: (visible) => {
    if (!visible) secret.value = ''
  },
})
const botsById = computed(() => new Map(bots.value.map((bot) => [bot.id, bot])))

function emptyForm() {
  return { label: '', bot_ids: [], scopes: [], expires_in_seconds: defaultLifetime }
}

function botLabel(bot) {
  return `${bot.name} (#${bot.id})`
}

function botNames(row) {
  return row.bot_ids.map((id) => {
    const bot = botsById.value.get(id)
    return bot ? botLabel(bot) : `#${id}`
  })
}

function scopeText(row) {
  return row.scopes.map((permission) => t(scopeLabels[permission])).join(' · ')
}

function status(row) {
  if (row.revoked_at) return 'revoked'
  return Date.parse(row.expires_at) > now.value ? 'active' : 'expired'
}

async function load(nextOffset) {
  const request = scope.begin('list')
  if (!request) return
  loading.value = true
  error.value = ''
  try {
    const { data } = await api.get(endpoint, {
      params: { offset: nextOffset, limit: pageSize },
      signal: request.signal,
      _silent: true,
    })
    if (!request.isCurrent()) return
    items.value = data.items
    bots.value = data.bots
    offset.value = nextOffset
    now.value = Date.now()
  } catch {
    if (request.isCurrent()) error.value = 'configurationCredentials.loadFailed'
  } finally {
    if (request.isCurrent()) loading.value = false
    request.finish()
  }
}

function openCreate() {
  error.value = ''
  form.value = emptyForm()
  showCreate.value = true
}

function openAction(type, row) {
  error.value = ''
  rotateLifetime.value = defaultLifetime
  action.value = { type, row }
  showAction.value = true
}

async function mutate(send, after) {
  if (busy.value) return
  const request = scope.begin('mutation')
  if (!request) return
  busy.value = true
  error.value = ''
  try {
    const response = await send({ signal: request.signal, _silent: true })
    if (!request.isCurrent()) return
    after(response.data)
    await load(0)
  } catch {
    if (request.isCurrent()) error.value = 'auditCredentials.mutationFailed'
  } finally {
    if (request.isCurrent()) busy.value = false
    request.finish()
  }
}

function create() {
  if (!canCreate.value) return
  const body = { ...form.value, label: form.value.label.trim() }
  return mutate(
    (config) => api.post(endpoint, body, config),
    (data) => {
      showCreate.value = false
      secret.value = data.credential
    }
  )
}

function confirmAction() {
  if (!action.value) return
  const { type, row } = action.value
  const url = `${endpoint}/${encodeURIComponent(row.id)}`
  return mutate(
    (config) =>
      type === 'rotate'
        ? api.post(`${url}/rotate`, { expires_in_seconds: rotateLifetime.value }, config)
        : api.delete(url, config),
    (data) => {
      showAction.value = false
      action.value = null
      if (type === 'rotate') secret.value = data.credential
    }
  )
}

watch(
  () => [props.principalId, props.sessionRevision],
  ([principalId]) => {
    scope.enter(principalId)
    secret.value = ''
    items.value = []
    bots.value = []
    offset.value = 0
    showCreate.value = false
    showAction.value = false
    action.value = null
    form.value = emptyForm()
    loading.value = false
    busy.value = false
    error.value = ''
    load(0)
  },
  { immediate: true, flush: 'sync' }
)
let resizeObserver
onMounted(() => {
  const layout = () => {
    compact.value = container.value.clientWidth < 1125
  }
  layout()
  resizeObserver = new ResizeObserver(layout)
  resizeObserver.observe(container.value)
})
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  scope.dispose()
})
</script>

<style scoped>
.credential-toolbar,
.credential-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.credential-toolbar {
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}

.credential-toolbar p {
  margin: 0;
  color: var(--el-text-color-secondary);
}

.credential-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.credential-pagination {
  justify-content: flex-end;
  margin-top: 16px;
}

.credential-name,
.credential-details dd {
  overflow-wrap: anywhere;
}

.credential-summary,
.credential-details dt {
  color: var(--el-text-color-secondary);
}

.credential-summary {
  font-size: 0.875em;
}

.credential-details {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 6px 12px;
  margin: 0;
  padding: 8px 12px;
}

.credential-details dd {
  margin: 0;
}
</style>
