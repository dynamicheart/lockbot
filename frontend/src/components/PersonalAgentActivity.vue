<template>
  <details class="agent-activity">
    <summary>
      <span>Agent 活动</span>
      <span class="agent-count">近 {{ activity.minutes }} 分钟 · {{ activity.count }} 个活跃</span>
    </summary>
    <p v-if="!activity.items.length" class="activity-empty">暂无调用记录</p>
    <template v-else>
      <ul class="activity-list">
        <li v-for="item in activity.items" :key="item.label ?? ''">
          <strong>{{ item.label ?? '未命名客户端' }}</strong>
          <span>最近来源 IP {{ item.ip ?? '未知' }}</span>
          <span
            >最后活跃
            <time :datetime="item.lastSeen">{{ formatResourceTime(item.lastSeen) }}</time></span
          >
        </li>
      </ul>
      <p class="activity-note">来源 IP 为服务端观测值，可能是代理或共享出口地址。</p>
    </template>
  </details>
</template>

<script setup>
import { formatResourceTime } from '../utils/resourceTime'

defineProps({ activity: { type: Object, required: true } })
</script>

<style scoped>
.agent-activity {
  border-top: 1px solid var(--lb-border-color);
  margin-top: 12px;
  padding: 12px 0 0;
}
summary {
  cursor: pointer;
  font-size: 13px;
  width: fit-content;
}
.agent-count {
  color: var(--lb-text-secondary);
  margin-inline-start: 8px;
}
.activity-list {
  list-style: none;
  margin: 10px 0 0;
  padding: 0;
}
.activity-list li {
  align-items: center;
  display: grid;
  gap: 8px 20px;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.35fr) max-content;
  padding: 8px 0;
  border-bottom: 1px solid var(--lb-border-color);
  overflow-wrap: anywhere;
}
.activity-list li:last-child {
  border-bottom: 0;
}
.activity-list span,
.activity-note,
.activity-empty {
  color: var(--lb-text-secondary);
  font-size: 13px;
}
.activity-note,
.activity-empty {
  margin: 8px 0 0;
}
@media (max-width: 660px) {
  .activity-list li {
    grid-template-columns: 1fr;
    gap: 4px;
  }
}
</style>
