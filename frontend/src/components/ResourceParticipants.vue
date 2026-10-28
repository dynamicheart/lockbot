<template>
  <div class="resource-participants" :class="{ plain }">
    <div v-for="(user, index) in users" :key="user.user_id || index" class="participant">
      <span v-if="queued" class="participant-order">{{ index + 1 }}</span>
      <div class="participant-content">
        <div class="participant-heading">
          <span v-if="plain" class="participant-user">{{
            user.user_id || user.userId || user
          }}</span>
          <el-tag v-else size="small" effect="plain">{{
            user.user_id || user.userId || user
          }}</el-tag>
          <span class="participant-origin">{{ $t('clusterState.' + originKey(user)) }}</span>
          <span v-if="taskFor(user)?.agent_label" class="participant-agent">{{
            taskFor(user).agent_label
          }}</span>
        </div>
        <div
          v-if="taskFor(user)?.purpose"
          class="participant-purpose"
          :title="$t('clusterState.usageHint')"
        >
          <details v-if="taskFor(user).purpose.length > 120">
            <summary>
              {{ taskFor(user).purpose.slice(0, 100) }}…
              <span class="purpose-expand">{{ $t('agentActivity.expandPurpose') }}</span>
            </summary>
            <div>{{ taskFor(user).purpose }}</div>
          </details>
          <template v-else>{{ taskFor(user).purpose }}</template>
        </div>
        <slot name="detail" :user="user" />
      </div>
    </div>
    <span v-if="!users.length" class="participant-empty">—</span>
  </div>
</template>

<script setup>
import { computed } from 'vue'
const props = defineProps({
  users: { type: Array, default: () => [] },
  resourceId: { type: String, required: true },
  usage: { type: Array, default: () => [] },
  queued: Boolean,
  plain: Boolean,
})
const tasks = computed(
  () =>
    new Map(
      props.usage
        .filter((item) => item.resource_id === props.resourceId)
        .map((item) => [item.actor.external_user_id, item])
    )
)
const originKey = (user) =>
  ({ chat: 'originChat', api: 'originAgent' })[user.source] || 'originUnknown'
const taskFor = (user) => tasks.value.get(user.user_id || user.userId || user)
</script>

<style scoped>
.resource-participants {
  display: grid;
  gap: 10px;
  min-width: 0;
  width: 100%;
}
.participant-user {
  font-size: 13px;
  font-weight: 500;
  overflow-wrap: anywhere;
}
.plain .participant-heading {
  gap: 4px 8px;
}
.plain .participant-origin {
  padding: 0;
  background: transparent;
  font-size: 12px;
}
.plain .participant-purpose {
  margin-top: 3px;
}
.participant {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  min-width: 0;
}
.participant-content {
  min-width: 0;
}
.participant-heading {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.participant-origin {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
  border-radius: 3px;
  padding: 1px 5px;
}
.participant-agent {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow-wrap: anywhere;
}
.participant-purpose {
  margin-top: 4px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--el-text-color-regular);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.participant-order,
.participant-empty {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 24px;
}
</style>

<style scoped>
.participant-purpose summary {
  cursor: pointer;
}
.purpose-expand {
  color: var(--el-color-primary);
  font-size: 12px;
}
</style>
