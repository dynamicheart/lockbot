<template>
  <section v-if="authorized" class="audit-credentials">
    <div class="credential-toolbar">
      <span>{{ $t('auditCredentials.scopeHint') }}</span>
      <el-button :disabled="denied || busy || loading" @click="load()">{{
        $t('common.refresh')
      }}</el-button>
    </div>
    <el-form label-position="top" class="credential-form" @submit.prevent="create">
      <el-form-item :label="$t('auditCredentials.name')">
        <el-input v-model="name" maxlength="64" :disabled="denied || busy" />
      </el-form-item>
      <el-form-item :label="$t('auditCredentials.bots')">
        <el-select
          v-model="botIds"
          :placeholder="$t('auditCredentials.selectBots')"
          multiple
          filterable
          :disabled="denied || busy"
        >
          <el-option
            v-for="bot in bots"
            :key="bot.id"
            :value="bot.id"
            :label="`${bot.name} #${bot.id}`"
          />
        </el-select>
      </el-form-item>
      <el-form-item :label="$t('auditCredentials.lifetime')">
        <el-select v-model="ttl" :disabled="denied || busy">
          <el-option
            v-for="value in lifetimes"
            :key="value"
            :value="value"
            :label="$t(`auditCredentials.ttl${value}`)"
          />
        </el-select>
      </el-form-item>
      <el-button type="primary" :disabled="!canCreate" :loading="creating" @click="create">
        {{ $t('auditCredentials.create') }}
      </el-button>
    </el-form>
    <p v-if="error" role="alert" class="credential-error">{{ $t(error) }}</p>
    <el-table v-loading="loading" :data="items" :empty-text="$t('common.noData')" stripe>
      <el-table-column prop="name" :label="$t('auditCredentials.name')" min-width="160" />
      <el-table-column :label="$t('auditCredentials.bots')" min-width="140">
        <template #default="{ row }">{{ row.bot_ids.map((id) => `#${id}`).join(', ') }}</template>
      </el-table-column>
      <el-table-column prop="scope" :label="$t('auditCredentials.scope')" min-width="150" />
      <el-table-column :label="$t('auditCredentials.expires')" min-width="180">
        <template #default="{ row }">{{ new Date(row.expires_at).toLocaleString() }}</template>
      </el-table-column>
      <el-table-column :label="$t('auditCredentials.status')" width="100">
        <template #default="{ row }">{{ $t(`auditCredentials.${status(row)}`) }}</template>
      </el-table-column>
      <el-table-column width="100">
        <template #default="{ row }">
          <el-popconfirm
            v-if="!row.revoked_at"
            :title="$t('auditCredentials.revokeConfirm')"
            :confirm-button-text="$t('common.confirm')"
            :cancel-button-text="$t('common.cancel')"
            @confirm="revoke(row.id)"
          >
            <template #reference>
              <el-button link type="danger" :disabled="denied || busy">{{
                $t('auditCredentials.revoke')
              }}</el-button>
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>
    <div class="credential-pagination">
      <el-button :disabled="denied || busy || loading || offset === 0" @click="changePage(-1)">{{
        $t('auditCredentials.previous')
      }}</el-button>
      <span>{{ offset / pageSize + 1 }}</span>
      <el-button
        :disabled="denied || busy || loading || items.length < pageSize"
        @click="changePage(1)"
        >{{ $t('auditCredentials.next') }}</el-button
      >
    </div>
    <el-dialog
      :model-value="!!secret"
      :title="$t('auditCredentials.shownOnce')"
      width="600px"
      @close="clearSecret"
    >
      <p>{{ $t('auditCredentials.saveHint') }}</p>
      <el-input :model-value="secret" type="password" show-password readonly autocomplete="off" />
      <template #footer>
        <el-button @click="copySecret">{{ $t('common.copy') }}</el-button>
        <el-button type="primary" @click="clearSecret">{{
          $t('auditCredentials.saved')
        }}</el-button>
      </template>
    </el-dialog>
  </section>
</template>
<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import api from '../utils/api'
import { createBotDetailScope } from '../utils/botDetailScope'
import { useHelpers } from '../utils/helpers'

const props = defineProps({
  authorized: Boolean,
  principalId: { type: [Number, String], default: '' },
})
const { copyText } = useHelpers()
const scope = createBotDetailScope()
const lifetimes = [600, 3600, 86400, 604800]
const pageSize = 50
const name = ref('')
const botIds = ref([])
const ttl = ref(86400)
const bots = ref([])
const items = ref([])
const offset = ref(0)
const secret = ref('')
const error = ref('')
const denied = ref(false)
const loading = ref(false)
const creating = ref(false)
const revoking = ref(false)
const now = ref(Date.now())
let clock
onMounted(() => {
  clock = setInterval(() => {
    now.value = Date.now()
  }, 30000)
})
const busy = computed(() => creating.value || revoking.value || !!secret.value)
const canCreate = computed(
  () =>
    props.authorized &&
    !denied.value &&
    !busy.value &&
    !loading.value &&
    name.value.trim().length > 0 &&
    name.value.trim().length <= 64 &&
    lifetimes.includes(ttl.value) &&
    botIds.value.length > 0 &&
    botIds.value.length <= 100 &&
    new Set(botIds.value).size === botIds.value.length &&
    botIds.value.every((id) => bots.value.some((bot) => bot.id === id))
)
const fields = ['id', 'name', 'scope', 'bot_ids', 'expires_at', 'revoked_at']
function safeMetadata(row) {
  if (
    !row ||
    typeof row.id !== 'string' ||
    !/^[A-Za-z0-9_-]{22}$/.test(row.id) ||
    typeof row.name !== 'string' ||
    row.scope !== 'task_notes:read' ||
    !Array.isArray(row.bot_ids) ||
    !row.bot_ids.every((id) => Number.isInteger(id) && id > 0) ||
    !Number.isFinite(Date.parse(row.expires_at))
  )
    return null
  return Object.fromEntries(fields.map((field) => [field, row[field]]))
}
function clearSecret() {
  secret.value = ''
}
function copySecret() {
  if (props.authorized && !denied.value && secret.value) copyText(secret.value)
}
function status(row) {
  return row.revoked_at ? 'revoked' : Date.parse(row.expires_at) <= now.value ? 'expired' : 'active'
}
function fail(err, mutation = false) {
  const code = err.response?.status
  if (mutation || code === 401 || code === 403) clearSecret()
  error.value =
    code === 401
      ? 'auth.sessionExpired'
      : code === 403
        ? 'common.permissionDenied'
        : mutation
          ? 'auditCredentials.mutationFailed'
          : 'common.requestFailed'
  if (code === 401 || code === 403) {
    denied.value = true
    scope.enter('')
    items.value = []
    bots.value = []
    botIds.value = []
    name.value = ''
    loading.value = creating.value = revoking.value = false
  }
}
async function load(internal = false) {
  if (!props.authorized || denied.value || (busy.value && internal !== true)) return
  const request = scope.begin('list')
  if (!request) return
  loading.value = true
  error.value = ''
  now.value = Date.now()
  try {
    const [grants, available] = await Promise.all([
      api.get('/admin/audit-credentials', {
        params: { limit: pageSize, offset: offset.value },
        signal: request.signal,
        _silent: true,
      }),
      api.get('/admin/bots', { signal: request.signal, _silent: true }),
    ])
    if (!request.isCurrent()) return
    items.value = Array.isArray(grants.data?.items)
      ? grants.data.items.map(safeMetadata).filter(Boolean)
      : []
    bots.value = Array.isArray(available.data)
      ? available.data
          .filter((bot) => Number.isInteger(bot.id) && bot.id > 0 && typeof bot.name === 'string')
          .map(({ id, name }) => ({ id, name }))
      : []
  } catch (err) {
    if (request.isCurrent()) fail(err)
  } finally {
    if (request.isCurrent()) loading.value = false
    request.finish()
  }
}
async function create() {
  if (!canCreate.value) return
  const request = scope.begin('create')
  if (!request) return
  const body = {
    name: name.value.trim(),
    bot_ids: [...botIds.value],
    expires_in_seconds: ttl.value,
  }
  creating.value = true
  error.value = ''
  clearSecret()
  try {
    const { data } = await api.post('/admin/audit-credentials', body, {
      signal: request.signal,
      _silent: true,
    })
    if (!request.isCurrent()) return
    const metadata = safeMetadata(data)
    if (
      !metadata ||
      data.shown_once !== true ||
      !/^lba1\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}$/.test(data.credential || '') ||
      data.credential.split('.')[1] !== metadata.id ||
      JSON.stringify([...metadata.bot_ids].sort((a, b) => a - b)) !==
        JSON.stringify([...body.bot_ids].sort((a, b) => a - b))
    ) {
      error.value = 'auditCredentials.mutationFailed'
      return
    }
    secret.value = data.credential
    name.value = ''
    botIds.value = []
    offset.value = 0
    await load(true)
  } catch (err) {
    if (request.isCurrent()) fail(err, true)
  } finally {
    if (request.isCurrent()) creating.value = false
    request.finish()
  }
}
async function revoke(id) {
  if (
    !props.authorized ||
    denied.value ||
    busy.value ||
    !items.value.some((row) => row.id === id && !row.revoked_at)
  )
    return
  const request = scope.begin('revoke')
  if (!request) return
  revoking.value = true
  error.value = ''
  clearSecret()
  try {
    await api.delete(`/admin/audit-credentials/${encodeURIComponent(id)}`, {
      signal: request.signal,
      _silent: true,
    })
    if (request.isCurrent()) await load(true)
  } catch (err) {
    if (request.isCurrent()) fail(err, true)
  } finally {
    if (request.isCurrent()) revoking.value = false
    request.finish()
  }
}
function changePage(direction) {
  if (!props.authorized || denied.value || busy.value || loading.value) return
  clearSecret()
  offset.value = Math.max(0, offset.value + direction * pageSize)
  return load()
}
watch(
  () => [props.authorized, props.principalId],
  () => {
    scope.enter(props.authorized ? String(props.principalId) : '')
    clearSecret()
    items.value = []
    bots.value = []
    botIds.value = []
    name.value = ''
    ttl.value = 86400
    offset.value = 0
    error.value = ''
    denied.value = false
    loading.value = creating.value = revoking.value = false
    if (props.authorized) load()
  },
  { immediate: true, flush: 'sync' }
)
onBeforeUnmount(() => {
  clearInterval(clock)
  scope.dispose()
  clearSecret()
})
</script>
<style scoped>
.credential-toolbar,
.credential-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}
.credential-toolbar > span {
  color: var(--lb-text-secondary);
  font-size: 13px;
}
.credential-form {
  display: grid;
  grid-template-columns: minmax(140px, 1fr) minmax(200px, 2fr) 150px auto;
  align-items: end;
  gap: 16px;
}
.credential-form .el-form-item {
  margin-bottom: 0;
}
.credential-form .el-select {
  width: 100%;
}
.credential-error {
  color: var(--el-color-danger);
}
.credential-pagination {
  justify-content: flex-end;
  margin-top: 16px;
}
@media (max-width: 800px) {
  .credential-form {
    grid-template-columns: 1fr;
  }
}
</style>
