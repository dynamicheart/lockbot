<template>
  <div class="resource-toolbar">
    <span v-if="showOverview" class="resource-overview">{{
      $t('resourceView.overview', totals)
    }}</span>
    <h2 v-else class="resource-table-title">{{ $t('resourceView.title') }}</h2>
    <div class="resource-filters">
      <el-input v-model="search" clearable :placeholder="$t('resourceView.search')" />
      <el-select v-model="filter" :aria-label="$t('resourceView.filter')">
        <el-option
          v-for="kind in ['all', 'idle', 'busy', 'queued']"
          :key="kind"
          :value="kind"
          :label="$t('resourceView.' + kind)"
        />
      </el-select>
    </div>
  </div>
  <el-table
    :data="visibleResources"
    row-key="id"
    class="resource-status-list desktop-resource-list"
  >
    <el-table-column :label="$t('resourceView.resource')" width="180">
      <template #default="{ row }">
        <div class="resource-identity">
          <div class="node-identity">
            <strong>{{ row.id }}</strong
            ><span>{{ row.alias }}</span>
          </div>
          <el-tag size="small" :type="isIdleResource(row) ? 'success' : 'warning'">{{
            status(row)
          }}</el-tag>
        </div>
      </template>
    </el-table-column>
    <el-table-column :label="$t('resourceView.currentHolder')" min-width="230">
      <template #default="{ row }">
        <ResourceParticipants
          :users="users(row.holders)"
          :resource-id="row.id"
          :usage="usage"
          plain
          purpose-on-hover
        />
      </template>
    </el-table-column>
    <el-table-column :label="$t('resourceView.queue')" min-width="320">
      <template #default="{ row }">
        <template v-if="row.booking_list?.length">
          <ResourceParticipants
            :users="users(row.booking_list)"
            :resource-id="row.id"
            :usage="usage"
            queued
            plain
            purpose-on-hover
          >
            <template #detail="{ user }"
              ><span class="queue-time">{{ queueTime(user, row) }}</span></template
            >
          </ResourceParticipants>
        </template>
        <span v-else class="empty-value">—</span>
      </template>
    </el-table-column>
    <el-table-column :label="$t('resourceView.remainingTime')" width="90" align="right">
      <template #default="{ row }">
        <div
          v-for="user in row.holders"
          :key="user.external_user_id || user.user_id"
          class="remaining"
        >
          {{ remaining(user) }}
        </div>
        <span v-if="!row.holders.length" class="empty-value">—</span>
      </template>
    </el-table-column>
  </el-table>
  <div class="mobile-resource-list">
    <article v-for="row in visibleResources" :key="row.id" class="mobile-resource-card">
      <div class="resource-identity">
        <div class="node-identity">
          <strong>{{ row.id }}</strong
          ><span>{{ row.alias }}</span>
        </div>
        <el-tag size="small" :type="isIdleResource(row) ? 'success' : 'warning'">{{
          status(row)
        }}</el-tag>
      </div>
      <ResourceParticipants
        :users="users(row.holders)"
        :resource-id="row.id"
        :usage="usage"
        purpose-on-hover
      >
        <template #detail="{ user }"
          ><span class="source">{{ remaining(user) }}</span></template
        >
      </ResourceParticipants>
      <details v-if="row.booking_list?.length" class="mobile-queue" open>
        <summary>{{ $t('resourceView.queuedCount', { count: row.booking_list.length }) }}</summary>
        <ResourceParticipants
          :users="users(row.booking_list)"
          :resource-id="row.id"
          :usage="usage"
          queued
          purpose-on-hover
        >
          <template #detail="{ user }"
            ><span class="queue-time">{{ queueTime(user, row) }}</span></template
          >
        </ResourceParticipants>
      </details>
    </article>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import ResourceParticipants from './ResourceParticipants.vue'
import { resourceStatus, isIdleResource } from '../utils/resourceAvailability'

const props = defineProps({
  resources: { type: Array, default: () => [] },
  usage: { type: Array, default: () => [] },
  serverOffset: { type: Number, default: 0 },
  showOverview: { type: Boolean, default: true },
})
const { t } = useI18n()
const search = ref('')
const filter = ref('all')
const totals = computed(() => ({
  total: props.resources.length,
  idle: props.resources.filter(isIdleResource).length,
  busy: props.resources.filter((row) => row.lock_status !== 'idle').length,
  queued: props.resources.reduce((sum, row) => sum + (row.booking_list?.length || 0), 0),
}))
const visibleResources = computed(() =>
  props.resources.filter((row) => {
    if (filter.value === 'idle' && !isIdleResource(row)) return false
    if (filter.value === 'busy' && row.lock_status === 'idle') return false
    if (filter.value === 'queued' && !row.booking_list?.length) return false
    const query = search.value.trim().toLocaleLowerCase()
    if (!query) return true
    const text = [
      row.id,
      row.alias,
      ...[...row.holders, ...(row.booking_list || [])].flatMap((user) => [
        user.external_user_id || user.user_id,
        task(row, user)?.agent_label,
        task(row, user)?.purpose,
      ]),
    ]
      .filter(Boolean)
      .join(' ')
      .toLocaleLowerCase()
    return text.includes(query)
  })
)
function queueTime(user, row) {
  const position =
    user.position ??
    row.booking_list.findIndex(
      (entry) => (entry.external_user_id || entry.user_id) === user.user_id
    )
  if (position === 0 && row.lock_status === 'idle') return t('resourceView.claimReady')
  const waiting =
    user.estimated_wait_seconds ??
    Math.max(
      0,
      ...row.holders.map((holder) => (expiry(holder) - clock.value - props.serverOffset) / 1000)
    ) +
      row.booking_list
        .slice(0, Math.max(0, position))
        .reduce((sum, entry) => sum + (entry.duration_seconds ?? entry.duration ?? 0), 0)
  const minutes = Math.ceil(waiting / 60)
  const time = shortMinutes(minutes)
  const booked = user.duration_seconds ?? user.duration
  return booked != null
    ? t('resourceView.bookingWait', { duration: shortMinutes(Math.ceil(booked / 60)), wait: time })
    : t('resourceView.estimatedWait', { time })
}
const clock = ref(Date.now())
const tick = setInterval(() => (clock.value = Date.now()), 1000)
onBeforeUnmount(() => clearInterval(tick))
const users = (items = []) => items.map((u) => ({ ...u, user_id: u.external_user_id || u.user_id }))
const task = (row, user) =>
  user.usage ||
  props.usage.find(
    (item) =>
      item.resource_id === row.id &&
      item.actor.external_user_id === (user.external_user_id || user.user_id)
  )
const status = (row) => t('resourceView.' + resourceStatus(row))
function expiry(user) {
  return user.expires_at
    ? Date.parse(user.expires_at)
    : ((user.start_time || 0) + (user.duration || 0)) * 1000
}
function remaining(user) {
  if (!expiry(user)) return ''
  const minutes = Math.ceil(Math.max(0, expiry(user) - clock.value - props.serverOffset) / 60000)
  return shortMinutes(minutes)
}
function shortMinutes(minutes) {
  if (minutes < 60) return `${minutes}m`
  const rest = minutes % 60
  return `${Math.floor(minutes / 60)}h${rest ? ` ${rest}m` : ''}`
}
</script>

<style scoped>
.resource-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.resource-overview,
.queue-time {
  font-size: 12px;
  color: var(--lb-text-secondary);
}
.queue-time {
  display: block;
  margin-top: 4px;
}
.resource-filters {
  display: flex;
  gap: 10px;
}
.resource-filters .el-input {
  width: 240px;
}
.resource-filters .el-select {
  width: 120px;
}
@media (max-width: 600px) {
  .resource-filters {
    width: 100%;
  }
  .resource-filters .el-input {
    flex: 1;
    width: auto;
  }
}

.resource-table-title {
  font-size: 16px;
  font-weight: 500;
  margin: 0;
  color: var(--lb-text-primary);
}
.holder-name {
  font-size: 14px;
  font-weight: 500;
  overflow-wrap: anywhere;
}
.resource-status-list {
  font-size: 14px;
}
.resource-status-list :deep(.el-table__row) {
  background: var(--lb-bg-card);
}
.resource-status-list :deep(.cell) {
  padding: 0 12px;
}
.resource-status-list :deep(th.el-table__cell) {
  background: var(--el-fill-color-light);
  color: var(--lb-text-secondary);
  font-weight: 500;
}
.resource-status-list :deep(td.el-table__cell) {
  padding: 16px 0;
  vertical-align: top;
}
.node-identity {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.desktop-resource-list .resource-identity {
  display: grid;
  align-items: center;
  justify-items: start;
  gap: 7px;
}
.resource-identity .el-tag {
  flex-shrink: 0;
}
.node-identity strong {
  font-size: 15px;
  color: var(--lb-text-primary);
}
.node-identity span,
.source,
.remaining {
  font-size: 12px;
  color: var(--lb-text-secondary);
}
.remaining {
  white-space: nowrap;
}
.source {
  display: block;
  margin-top: 4px;
}
.empty-value {
  color: var(--el-text-color-placeholder);
}
.mobile-resource-list {
  display: none;
}
@media (max-width: 860px) {
  .desktop-resource-list {
    display: none;
  }
  .mobile-resource-list {
    display: grid;
    gap: 12px;
  }
  .mobile-resource-card {
    padding: 16px;
    border: 1px solid var(--lb-border-color);
    border-radius: 8px;
    background: var(--lb-bg-card);
  }
  .mobile-resource-card > .resource-identity {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 14px;
  }
  .mobile-queue {
    margin-top: 16px;
  }
  .mobile-queue summary {
    font-size: 13px;
    color: var(--el-color-primary);
    cursor: pointer;
    margin-bottom: 12px;
  }
}
</style>
