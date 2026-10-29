<template>
  <section class="quick-overview" :aria-label="$t('resourceView.quickOverview')">
    <h2>{{ $t('resourceView.quickOverview') }}</h2>
    <div class="quick-groups">
      <section
        v-for="group in groups"
        :key="group.key"
        class="quick-group"
        :class="{ 'idle-group': group.key === 'idle' }"
      >
        <h3>
          {{ group.label }} <span>{{ group.rows.length }}</span>
        </h3>
        <div v-if="group.key === 'idle' && group.rows.length" class="idle-resources">
          <div
            v-for="machine in idleMachines"
            :key="machine.id"
            class="idle-machine"
            :class="{ 'node-only': !machine.hasCards }"
          >
            <span v-if="machine.hasCards" class="idle-machine-name">{{ machine.name }}</span>
            <button
              v-for="row in machine.rows"
              :key="row.id"
              type="button"
              class="resource-chip"
              :class="{ selected: selectedId === row.id }"
              :aria-label="row.display_name || row.id"
              :title="row.display_name || row.id"
              @click="$emit('select', row.id)"
            >
              {{ cardName(row) || nodeName(row) }}
            </button>
          </div>
        </div>
        <div v-else-if="group.rows.length" class="quick-grid">
          <button
            v-for="row in group.rows"
            :key="row.id"
            type="button"
            class="resource-chip"
            :class="{ selected: selectedId === row.id, idle: isIdleResource(row) }"
            :title="`${row.display_name || row.id} · ${owners(row)}`"
            @click="$emit('select', row.id)"
          >
            <span class="resource-name"
              ><strong>{{ nodeName(row) }}</strong
              ><span v-if="cardName(row)">{{ cardName(row) }}</span></span
            >
            <span class="resource-owner">{{ owners(row) }}</span>
            <span class="queue-count" :class="{ waiting: row.booking_list?.length }">{{
              $t('resourceView.queueBadge', { count: row.booking_list?.length || 0 })
            }}</span>
          </button>
        </div>
        <span v-else class="group-empty">—</span>
      </section>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { isIdleResource } from '../utils/resourceAvailability'
const props = defineProps({
  resources: { type: Array, default: () => [] },
  selectedId: { type: String, default: '' },
})
defineEmits(['select'])
const { t } = useI18n()
const groups = computed(() => [
  { key: 'idle', label: t('resourceView.idle'), rows: props.resources.filter(isIdleResource) },
  {
    key: 'busy',
    label: t('resourceView.busyAndQueued'),
    rows: props.resources
      .filter((row) => !isIdleResource(row))
      .sort((a, b) => (a.booking_list?.length || 0) - (b.booking_list?.length || 0)),
  },
])
const cardName = (row) => row.id.match(/:dev([0-9]+)$/)?.[0].slice(1) || ''
const nodeName = (row) =>
  cardName(row) ? row.id.slice(0, row.id.lastIndexOf(':dev')) : row.display_name || row.id
const idleMachines = computed(() => {
  const machines = new Map()
  for (const row of props.resources.filter(isIdleResource)) {
    const name = nodeName(row)
    const id = cardName(row) ? name : row.id
    if (!machines.has(id)) machines.set(id, { id, name, hasCards: !!cardName(row), rows: [] })
    machines.get(id).rows.push(row)
  }
  return [...machines.values()]
})
function owners(row) {
  const names = row.holders.map((user) => user.external_user_id || user.user_id).filter(Boolean)
  return names.length
    ? names.join(', ')
    : t(isIdleResource(row) ? 'resourceView.idle' : 'resourceView.pending')
}
</script>

<style scoped>
.quick-overview {
  padding-bottom: 20px;
  margin-bottom: 22px;
  border-bottom: 1px solid var(--lb-border-color);
}
h2 {
  margin: 0 0 16px;
  font-size: 15px;
  color: var(--lb-text-primary);
}
.quick-groups {
  padding-right: 2px;
}
.quick-group + .quick-group {
  margin-top: 16px;
}
h3 {
  margin: 0 0 8px;
  font-size: 12px;
  font-weight: 500;
  color: var(--lb-text-secondary);
}
h3 span {
  margin-left: 6px;
  font-variant-numeric: tabular-nums;
}
.quick-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 8px;
}
.idle-resources {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 8px;
}
.idle-machine {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
  max-width: 100%;
  box-sizing: border-box;
  padding: 8px 12px;
  border: 1px solid var(--lb-border-color);
  border-radius: 4px;
  font-size: 12px;
}
.idle-machine-name {
  font-weight: 600;
  overflow-wrap: anywhere;
}
.idle-machine .resource-chip {
  display: inline-flex;
  min-width: 0;
  max-width: 100%;
  padding: 3px 7px;
  border-color: transparent;
  background: var(--el-fill-color-light);
  color: var(--lb-text-secondary);
  overflow-wrap: anywhere;
}
.idle-machine.node-only {
  padding: 0;
  border: 0;
}
.idle-machine.node-only .resource-chip {
  padding: 10px 12px;
  border-color: var(--lb-border-color);
  background: var(--lb-bg-card);
  color: var(--lb-text-primary);
  font-weight: 600;
}
.idle-machine .resource-chip:hover,
.idle-machine .resource-chip:focus-visible,
.idle-machine .resource-chip.selected {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.resource-chip {
  display: grid;
  grid-template-columns: minmax(80px, 1fr) minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  text-align: left;
  border: 1px solid var(--lb-border-color);
  border-radius: 4px;
  background: var(--lb-bg-card);
  color: var(--lb-text-primary);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
}
.resource-chip:hover,
.resource-chip:focus-visible,
.resource-chip.selected {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  outline: none;
}
.resource-name {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
}
.resource-name strong,
.resource-owner {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.resource-name strong {
  font-weight: 600;
}
.resource-name > span {
  white-space: nowrap;
  color: var(--lb-text-secondary);
}
.resource-owner {
  color: var(--lb-text-secondary);
}
.queue-count {
  padding: 2px 5px;
  white-space: nowrap;
  border-radius: 3px;
  background: var(--el-fill-color-light);
  color: var(--lb-text-secondary);
  font-size: 11px;
}
.queue-count.waiting {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
}
.group-empty {
  font-size: 12px;
  color: var(--lb-text-secondary);
}
@media (max-width: 560px) {
  .quick-grid {
    grid-template-columns: 1fr;
  }
}
</style>
