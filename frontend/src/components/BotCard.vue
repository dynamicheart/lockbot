<template>
  <el-card shadow="hover" class="bot-card" @click="$router.push(`/bots/${bot.id}`)">
    <div class="card-top">
      <div>
        <div class="bot-name">{{ bot.name }}</div>
        <div class="bot-id">ID: {{ bot.id }}</div>
      </div>
      <StatusBadge :status="bot.status" />
    </div>
    <div class="card-body">
      <el-tag size="small" type="primary" effect="plain">{{ bot.bot_type }}</el-tag>
      <el-tag size="small" type="info" effect="plain">{{ bot.platform }}</el-tag>
    </div>
    <div class="card-stats">
      <span class="stat-item">
        <el-icon size="13"><Monitor /></el-icon>
        {{ resourceLabel }}
      </span>
      <div v-if="stats.utilization" class="usage-bar-container">
        <div class="usage-bar">
          <div class="usage-bar-fill idle" :style="{ width: idlePercent + '%' }"></div>
          <div class="usage-bar-fill inuse" :style="{ width: inUsePercent + '%' }"></div>
        </div>
        <span class="usage-label">{{ stats.utilization.inUse }}/{{ stats.utilization.total }}</span>
      </div>
      <span v-else class="usage-label">-</span>
    </div>
    <div class="card-footer">
      <span v-if="bot.last_user_id" class="last-active">
        {{ $t('botCard.lastActive') }}: {{ bot.last_user_id }}
      </span>
      <span v-if="bot.group_id" class="group-ids" @click.stop>
        <el-tag v-for="gid in bot.group_id.split(',')" :key="gid" size="small" effect="plain" class="group-tag" @click="copyText(gid)">{{ gid }}</el-tag>
      </span>
    </div>
  </el-card>
</template>

<script setup>
import { computed } from 'vue'
import { Monitor } from '@element-plus/icons-vue'
import StatusBadge from './StatusBadge.vue'
import { useBotsStore } from '../stores/bots'
import { useHelpers } from '../utils/helpers'

const props = defineProps({
  bot: { type: Object, required: true },
})

const botsStore = useBotsStore()
const { copyText } = useHelpers()

const stats = computed(() => botsStore.computeBotStats(props.bot))

const resourceLabel = computed(() => {
  const rc = stats.value.resourceCounts
  if (props.bot.bot_type === 'DEVICE') {
    return `${rc.nodes}n / ${rc.devices}d`
  }
  return `${rc.nodes} nodes`
})

const idlePercent = computed(() => {
  const u = stats.value.utilization
  if (!u || u.total === 0) return 100
  return Math.round((u.idle / u.total) * 100)
})

const inUsePercent = computed(() => {
  const u = stats.value.utilization
  if (!u || u.total === 0) return 0
  return Math.round((u.inUse / u.total) * 100)
})



</script>

<style scoped>
.bot-card {
  cursor: pointer;
  transition: transform 0.2s, box-shadow 0.2s;
}
.bot-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--lb-shadow-card-hover);
}
.card-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.bot-name {
  font-size: 16px;
  font-weight: 600;
  color: var(--lb-text-primary);
}
.bot-id {
  font-size: 12px;
  color: var(--lb-text-secondary);
  margin-top: 2px;
}
.card-body {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.card-stats {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  font-size: 12px;
  color: var(--lb-text-secondary);
}
.stat-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}
.usage-bar-container {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
}
.usage-bar {
  flex: 1;
  height: 6px;
  border-radius: 3px;
  background: var(--el-fill-color-light);
  overflow: hidden;
  display: flex;
}
.usage-bar-fill.idle {
  background: var(--el-color-info-light-5);
  transition: width 0.3s;
}
.usage-bar-fill.inuse {
  background: var(--el-color-success);
  transition: width 0.3s;
}
.usage-label {
  font-size: 11px;
  white-space: nowrap;
}
.card-footer {
  border-top: 1px solid var(--lb-border-light);
  padding-top: 8px;
  min-height: 30px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  overflow: hidden;
}
.group-ids {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.group-tag {
  cursor: pointer;
}
.last-active {
  font-size: 12px;
  color: var(--lb-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}
.last-active {
  text-align: right;
}
</style>
