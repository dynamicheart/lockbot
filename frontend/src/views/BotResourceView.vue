<template>
  <main class="resource-view">
    <section v-if="invalid" class="view-message">
      <el-icon :size="30"><Lock /></el-icon>
      <h1>{{ $t('resourceView.invalidTitle') }}</h1>
      <p>{{ $t('resourceView.invalidHint') }}</p>
    </section>
    <section v-else-if="!snapshot" class="view-message">
      <h1>{{ $t('resourceView.title') }}</h1>
      <p>{{ failed ? $t('resourceView.unavailable') : $t('common.loading') }}</p>
      <el-button v-if="failed" @click="refresh">{{ $t('common.refresh') }}</el-button>
    </section>
    <template v-else>
      <header class="view-header">
        <div>
          <h1>
            {{ snapshot.name }} <el-tag size="small" type="info">{{ snapshot.bot_type }}</el-tag>
          </h1>
          <p>{{ $t('resourceView.updated') }} {{ localTime(snapshot.captured_at) }}</p>
        </div>
        <el-button :loading="loading" @click="refresh"
          ><el-icon><Refresh /></el-icon>{{ $t('common.refresh') }}</el-button
        >
      </header>
      <div v-if="snapshot.health === 'running'" class="resource-summary">
        <div>
          <span>{{ $t('resourceView.totalResources') }}</span
          ><strong>{{ summary.total }}</strong>
        </div>
        <div>
          <span>{{ $t('resourceView.idleResources') }}</span
          ><strong class="summary-idle">{{ summary.idle }}</strong>
        </div>
        <div>
          <span>{{ $t('resourceView.queueReservations') }}</span
          ><strong>{{ summary.queued }}</strong>
        </div>
      </div>
      <p v-if="failed" class="view-status">{{ $t('resourceView.unavailable') }}</p>
      <el-empty v-if="snapshot.health !== 'running'" :description="$t('resourceView.stopped')" />
      <el-empty v-else-if="!snapshot.resources.length" :description="$t('resourceView.empty')" />
      <section v-else-if="!snapshot.resources[0]?.devices" class="resource-panel">
        <ResourceStatusList
          :resources="snapshot.resources"
          :usage="usage"
          :server-offset="serverOffset"
          :show-overview="false"
        />
      </section>
      <div v-else class="resource-grid">
        <article v-for="resource in snapshot.resources" :key="resource.id" class="resource-card">
          <div class="resource-heading">
            <h2>
              <el-icon><Monitor /></el-icon>{{ resource.id
              }}<span v-if="resource.alias" class="node-alias">{{ resource.alias }}</span>
            </h2>
            <el-tag size="small" :type="isIdleResource(resource) ? 'success' : 'warning'">{{
              statusText(resource)
            }}</el-tag>
          </div>
          <template v-if="resource.devices">
            <div v-for="device in resource.devices" :key="device.id" class="device-row">
              <div class="device-heading">
                <strong>dev{{ device.id }}</strong
                ><span>{{ device.dev_model }}</span
                ><el-tag size="small" :type="isIdleResource(device) ? 'success' : 'warning'">{{
                  statusText(device)
                }}</el-tag>
              </div>
              <ResourceParticipants
                :users="participants(device.holders)"
                :resource-id="device.resource_id || resource.id"
                :usage="usage"
              >
                <template #detail="{ user }"
                  ><span class="participant-time">{{ remaining(user.expires_at) }}</span></template
                >
              </ResourceParticipants>
              <template v-if="device.booking_list?.length">
                <h3>{{ $t('resourceView.queue') }} · {{ device.booking_list.length }}</h3>
                <ResourceParticipants
                  :users="participants(device.booking_list)"
                  :resource-id="device.resource_id || resource.id"
                  :usage="usage"
                  queued
                >
                  <template #detail="{ user }"
                    ><span class="participant-time">{{ bookingTime(user, device) }}</span></template
                  >
                </ResourceParticipants>
              </template>
            </div>
          </template>
          <template v-else>
            <ResourceParticipants
              :users="participants(resource.holders)"
              :resource-id="resource.id"
              :usage="usage"
            >
              <template #detail="{ user }"
                ><span class="participant-time">{{ remaining(user.expires_at) }}</span></template
              >
            </ResourceParticipants>
            <template v-if="resource.booking_list?.length">
              <h3>{{ $t('resourceView.queue') }} · {{ resource.booking_list.length }}</h3>
              <ResourceParticipants
                :users="participants(resource.booking_list)"
                :resource-id="resource.id"
                :usage="usage"
                queued
              >
                <template #detail="{ user }"
                  ><span class="participant-time">{{ bookingTime(user, resource) }}</span></template
                >
              </ResourceParticipants>
            </template>
          </template>
        </article>
      </div>
      <footer>{{ $t('resourceView.expires') }} {{ localTime(snapshot.view_expires_at) }}</footer>
    </template>
  </main>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import ResourceParticipants from '../components/ResourceParticipants.vue'
import ResourceStatusList from '../components/ResourceStatusList.vue'
import { resourceStatus, isIdleResource } from '../utils/resourceAvailability'

const route = useRoute()
const { t, locale } = useI18n()
const snapshot = ref(null)
const loading = ref(false)
const invalid = ref(false)
const failed = ref(false)
const clock = ref(Date.now())
let token = ''
let botId = ''
let deadline = 0
let serverOffset = 0
let controller = null
let requestEpoch = 0

function clear() {
  controller?.abort()
  requestEpoch++
  snapshot.value = null
  token = ''
  deadline = 0
  loading.value = false
  invalid.value = true
}
function expired() {
  if (deadline && Date.now() >= deadline) {
    clear()
    return true
  }
  return false
}
async function refresh() {
  if (!token || loading.value || document.hidden || expired()) return
  const epoch = ++requestEpoch
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  try {
    // Deliberately separate from the management Axios client: never send a stored user JWT.
    const response = await fetch(`/api/bot-views/${encodeURIComponent(botId)}/state`, {
      headers: { Authorization: `Bearer ${token}` },
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
      referrerPolicy: 'no-referrer',
      signal: controller.signal,
    })
    if (epoch !== requestEpoch) return
    if (response.status === 401 || response.status === 403 || response.status === 404) {
      clear()
      return
    }
    if (!response.ok) throw new Error('state_unavailable')
    const data = await response.json()
    if (epoch !== requestEpoch) return
    serverOffset = Date.parse(data.server_time) - Date.now()
    deadline = Date.parse(data.view_expires_at) - serverOffset
    if (expired()) return
    snapshot.value = data
    failed.value = false
    invalid.value = false
  } catch (error) {
    if (error.name !== 'AbortError' && epoch === requestEpoch) failed.value = true
  } finally {
    if (epoch === requestEpoch) loading.value = false
  }
}

watch(
  () => [route.params.id, route.query.view],
  ([id, supplied]) => {
    if (String(id) !== botId) clear()
    botId = String(id)
    if (typeof supplied === 'string' && supplied.length <= 160) {
      clear()
      token = supplied
      invalid.value = false
      // Keep the shared read-only code in the URL fragment so normal reloads work.
      // Fragments are not sent to the server; never persist a personal key or account JWT here.
      refresh()
    } else if (!token) invalid.value = true
  },
  { immediate: true }
)

const tick = setInterval(() => {
  clock.value = Date.now()
  expired()
}, 1000)
const poll = setInterval(refresh, 30000)
const visible = () => {
  if (!document.hidden && !expired()) refresh()
}
document.addEventListener('visibilitychange', visible)
onBeforeUnmount(() => {
  clearInterval(tick)
  clearInterval(poll)
  document.removeEventListener('visibilitychange', visible)
  clear()
})

const participants = (items = []) =>
  items.map((user) => ({ ...user, user_id: user.external_user_id }))
const usage = computed(() =>
  (snapshot.value?.resources || []).flatMap((resource) => {
    const entries = [resource, ...(resource.devices || [])]
    return entries.flatMap((entry) =>
      [...(entry.holders || []), ...(entry.booking_list || [])].map((u) => u.usage).filter(Boolean)
    )
  })
)
const summary = computed(() => {
  const resources = (snapshot.value?.resources || []).flatMap(
    (resource) => resource.devices || [resource]
  )
  return {
    total: resources.length,
    idle: resources.filter(isIdleResource).length,
    queued: resources.reduce((sum, row) => sum + (row.booking_list?.length || 0), 0),
  }
})
const localTime = (value) => (value ? new Date(value).toLocaleString(locale.value) : '—')
const statusText = (resource) => t('resourceView.' + resourceStatus(resource))
function duration(seconds) {
  const minutes = Math.ceil(Math.max(0, seconds) / 60)
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`
}
const remaining = (expires) =>
  expires
    ? t('resourceView.remaining', {
        time: duration((Date.parse(expires) - clock.value - serverOffset) / 1000),
      })
    : ''
function bookingTime(user, resource) {
  if (resource.lock_status === 'idle' && user.position === 0) return t('resourceView.claimReady')
  return user.estimated_wait_seconds != null
    ? t('resourceView.estimatedWait', { time: duration(user.estimated_wait_seconds) })
    : ''
}
</script>

<style scoped>
.resource-view {
  max-width: 1440px;
  margin: 0 auto;
  padding: 28px 24px;
}
.view-header,
.resource-heading,
.device-heading,
h2 {
  display: flex;
  align-items: center;
  gap: 10px;
}
.view-header,
.resource-heading {
  justify-content: space-between;
}
.view-header {
  margin-bottom: 24px;
}
.resource-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 22px;
}
.resource-summary > div {
  background: var(--lb-bg-card);
  border: 1px solid var(--lb-border-color);
  border-radius: 6px;
  padding: 18px 22px;
}
.resource-summary span {
  color: var(--lb-text-secondary);
  font-size: 13px;
  display: block;
}
.resource-summary strong {
  color: var(--lb-text-primary);
  font-size: 28px;
  font-weight: 600;
  line-height: 1.2;
  display: block;
  margin-top: 8px;
}
.resource-summary .summary-idle {
  color: var(--el-color-success);
}
.resource-panel {
  background: var(--lb-bg-card);
  border: 1px solid var(--lb-border-color);
  border-radius: 6px;
  padding: 20px;
}

h1 {
  font-size: 22px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 12px;
}
.view-header p,
footer,
.participant-time,
.device-heading > span,
.node-alias {
  color: var(--lb-text-secondary);
  font-size: 12px;
}
.view-header p {
  margin-top: 8px;
}
.resource-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(285px, 1fr));
  gap: 16px;
}
.resource-card {
  min-width: 0;
  background: var(--lb-bg-card);
  border: 1px solid var(--lb-border-color);
  border-radius: 10px;
  padding: 18px;
}
.resource-heading {
  margin-bottom: 14px;
}
h2 {
  font-size: 15px;
  min-width: 0;
  overflow-wrap: anywhere;
}
h3 {
  font-size: 12px;
  color: var(--lb-text-secondary);
  margin: 18px 0 10px;
  font-weight: 500;
}
.device-row + .device-row {
  margin-top: 16px;
  border-top: 1px solid var(--lb-border-color);
  padding-top: 14px;
}
.device-heading {
  margin-bottom: 10px;
}
.participant-time {
  display: block;
  margin-top: 4px;
}
footer {
  margin-top: 22px;
  text-align: right;
}
.view-message {
  max-width: 500px;
  margin: 18vh auto;
  padding: 36px;
  border: 1px solid var(--lb-border-color);
  border-radius: 12px;
  background: var(--lb-bg-card);
}
.view-message h1 {
  margin: 16px 0 12px;
  font-size: 20px;
}
.view-message p {
  font-size: 14px;
  color: var(--lb-text-secondary);
  line-height: 1.8;
  margin-bottom: 16px;
}
.view-status {
  color: var(--el-color-warning);
  margin-bottom: 16px;
}
@media (max-width: 600px) {
  .resource-view {
    padding: 18px 14px;
  }
  .view-header {
    align-items: flex-start;
  }
  h1 {
    font-size: 18px;
    flex-wrap: wrap;
  }
  .resource-grid {
    grid-template-columns: 1fr;
  }
  .resource-summary {
    gap: 8px;
  }
  .resource-summary > div {
    padding: 12px;
  }
  .resource-summary strong {
    font-size: 24px;
  }
  .resource-panel {
    padding: 14px;
  }
}
</style>
