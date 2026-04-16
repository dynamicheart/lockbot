<template>
  <div>
    <div class="node-list">
      <div
        v-for="(node, i) in nodes"
        :key="node.id"
        class="device-card"
        :class="{
          'device-card--dragging': dragIndex === i,
          'device-card--over-top': dropIndex === i && dropPos === 'top',
          'device-card--over-bottom': dropIndex === i && dropPos === 'bottom',
        }"
        @dragover.prevent="onDragOver(i, $event)"
        @dragleave="onDragLeave(i)"
        @drop.prevent="onDrop(i)"
      >
        <!-- Node header -->
        <div class="device-card-header">
          <div class="device-card-header-left">
            <span class="node-index">{{ i + 1 }}</span>
            <el-icon
              class="drag-handle"
              :size="18"
              draggable="true"
              @dragstart="onDragStart(i, $event)"
              @dragend="onDragEnd"
              ><Rank
            /></el-icon>
            <el-input
              v-model="node.name"
              :placeholder="$t('botForm.nodeNamePlaceholder')"
              :maxlength="6"
              :class="{ 'is-duplicate': isDuplicate(i), 'is-invalid': isInvalidName(i) }"
              class="node-name-input"
            />
            <span v-if="isDuplicate(i)" class="dup-tip">{{ $t('botForm.duplicateNode') }}</span>
            <span v-else-if="isInvalidName(i)" class="dup-tip">{{
              $t('botForm.nodeNameInvalid')
            }}</span>
            <span class="device-count-badge">{{ totalDevices(node) }}</span>
          </div>
          <div class="device-card-header-right">
            <el-button :icon="CopyDocument" text size="small" @click="copyNode(i)">{{
              $t('botForm.copyNode')
            }}</el-button>
            <el-button
              class="node-remove"
              :icon="Delete"
              text
              type="danger"
              :disabled="nodes.length <= 1"
              @click="removeNode(i)"
            />
          </div>
        </div>

        <!-- Device rows -->
        <div class="device-rows">
          <div v-for="(dev, j) in node.devices" :key="j" class="device-row">
            <el-autocomplete
              v-model="dev.model"
              :fetch-suggestions="
                (q, cb) =>
                  cb(
                    PRESET_MODELS.filter((m) => m.toLowerCase().includes(q.toLowerCase())).map(
                      (m) => ({ value: m })
                    )
                  )
              "
              :placeholder="$t('botForm.deviceModel')"
              class="device-select"
              clearable
              @select="() => mergeDevices(node)"
              @blur="handleDeviceModelBlur(dev, node)"
            />
            <el-input-number v-model="dev.count" :min="1" :max="64" class="device-count-input" />
            <el-button
              :icon="Delete"
              text
              type="danger"
              size="small"
              :disabled="node.devices.length <= 1"
              @click="node.devices.splice(j, 1)"
            />
          </div>
        </div>

        <div class="quick-add">
          <el-button
            class="add-device-btn"
            text
            type="primary"
            @click="node.devices.push({ model: '', count: 1 })"
          >
            <el-icon><Plus /></el-icon>
          </el-button>
          <span class="quick-add-label">{{ $t('botForm.quickAdd') }}</span>
          <el-button
            v-for="model in quickModels"
            :key="model"
            size="small"
            round
            effect="plain"
            @click="quickAddDevice(node, model)"
            >{{ model }}</el-button
          >
        </div>
      </div>
    </div>
    <el-button class="add-node-btn" style="margin-top: 12px" @click="addNode">
      <el-icon><Plus /></el-icon> {{ $t('botForm.addNode') }}
    </el-button>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import { Delete, Plus, Rank, CopyDocument } from '@element-plus/icons-vue'

let nodeIdSeq = 0
const PRESET_MODELS = ['h20', 'a800', 'p800']

const props = defineProps({
  modelValue: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['update:modelValue'])

const nodes = ref(parseInit())
const dragIndex = ref(-1)
const dropIndex = ref(-1)
const dropPos = ref('')
const quickModels = ['h20', 'a800', 'p800']

function parseInit() {
  const cfg = props.modelValue
  if (!cfg || typeof cfg !== 'object')
    return [{ id: ++nodeIdSeq, name: '', devices: [{ model: '', count: 1 }] }]
  const entries = Object.entries(cfg)
  if (entries.length === 0)
    return [{ id: ++nodeIdSeq, name: '', devices: [{ model: '', count: 1 }] }]
  return entries.map(([name, devices]) => ({
    id: ++nodeIdSeq,
    name,
    devices: Array.isArray(devices) ? groupDevices(devices) : [{ model: '', count: 1 }],
  }))
}

function groupDevices(arr) {
  if (!arr.length) return []
  const result = [{ model: arr[0], count: 1 }]
  for (let i = 1; i < arr.length; i++) {
    if (arr[i] === result[result.length - 1].model) {
      result[result.length - 1].count++
    } else {
      result.push({ model: arr[i], count: 1 })
    }
  }
  return result
}

function totalDevices(node) {
  return node.devices.reduce((sum, d) => sum + (d.count || 0), 0)
}

function handleDeviceModelBlur(dev, node) {
  dev.model = (dev.model || '').replace(/[^a-zA-Z0-9]/g, '').toLowerCase()
  mergeDevices(node)
}

function mergeDevices(node) {
  const devs = node.devices
  let i = 0
  while (i < devs.length - 1) {
    if (devs[i].model && devs[i].model === devs[i + 1].model) {
      devs[i].count += devs[i + 1].count
      devs.splice(i + 1, 1)
    } else {
      i++
    }
  }
}

const NODE_NAME_RE = /^[a-zA-Z0-9_-]*$/

function isDuplicate(i) {
  const val = nodes.value[i]?.name?.trim()
  if (!val) return false
  return nodes.value.some((n, j) => j !== i && n.name?.trim() === val)
}

function isInvalidName(i) {
  const val = nodes.value[i]?.name?.trim()
  if (!val) return false
  return !NODE_NAME_RE.test(val)
}

function addNode() {
  nodes.value.push({ id: ++nodeIdSeq, name: '', devices: [{ model: '', count: 1 }] })
}

function quickAddDevice(node, model) {
  const devs = node.devices
  // Normalize all existing models to lowercase first
  for (const d of devs) {
    if (d.model) d.model = d.model.toLowerCase()
  }
  const last = devs[devs.length - 1]
  if (last && last.model === model) {
    last.count++
  } else {
    const empty = devs.find((d) => !d.model)
    if (empty) empty.model = model
    else devs.push({ model, count: 1 })
  }
}

function copyNode(i) {
  const src = nodes.value[i]
  if (!src) return
  const newDevices = src.devices.map((d) => ({ ...d }))
  nodes.value.splice(i + 1, 0, { id: ++nodeIdSeq, name: '', devices: newDevices })
}

function removeNode(i) {
  if (nodes.value.length <= 1) return
  nodes.value.splice(i, 1)
}

function onDragStart(i, e) {
  dragIndex.value = i
  e.dataTransfer.effectAllowed = 'move'
  e.dataTransfer.setData('text/plain', '')
}

function onDragOver(i, e) {
  if (dragIndex.value === -1 || dragIndex.value === i) return
  const rect = e.currentTarget.getBoundingClientRect()
  const mid = rect.top + rect.height / 2
  dropIndex.value = i
  dropPos.value = e.clientY < mid ? 'top' : 'bottom'
}

function onDragLeave(i) {
  if (dropIndex.value === i) {
    dropIndex.value = -1
    dropPos.value = ''
  }
}

function onDrop(i) {
  const from = dragIndex.value
  if (from === -1 || from === i) return
  const target = dropPos.value === 'bottom' ? (from < i ? i : i + 1) : from < i ? i - 1 : i
  const item = nodes.value.splice(from, 1)[0]
  nodes.value.splice(target, 0, item)
  dragIndex.value = -1
  dropIndex.value = -1
  dropPos.value = ''
}

function onDragEnd() {
  dragIndex.value = -1
  dropIndex.value = -1
  dropPos.value = ''
}

watch(
  nodes,
  () => {
    const result = {}
    for (const node of nodes.value) {
      if (!node.name || !NODE_NAME_RE.test(node.name.trim())) continue
      const devices = []
      for (const dev of node.devices) {
        if (dev.model) {
          const m = dev.model.toLowerCase()
          for (let k = 0; k < dev.count; k++) devices.push(m)
        }
      }
      result[node.name] = devices
    }
    emit('update:modelValue', result)
  },
  { deep: true }
)
</script>

<style scoped>
.node-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.device-card {
  border: 1px solid var(--lb-border-light);
  border-radius: 10px;
  padding: 14px 16px;
  background: var(--el-bg-color);
  transition:
    opacity 0.2s,
    box-shadow 0.2s;
  position: relative;
}
.device-card:hover {
  box-shadow: var(--lb-shadow-card-hover, 0 2px 8px rgba(0, 0, 0, 0.06));
}
.device-card--dragging {
  opacity: 0.4;
}
.device-card--over-top::before {
  content: '';
  position: absolute;
  top: -3px;
  left: 8px;
  right: 8px;
  height: 2px;
  border-radius: 1px;
  background: var(--el-color-primary);
}
.device-card--over-bottom::after {
  content: '';
  position: absolute;
  bottom: -3px;
  left: 8px;
  right: 8px;
  height: 2px;
  border-radius: 1px;
  background: var(--el-color-primary);
}
.device-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}
.device-card-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
}
.node-index {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--el-fill-color);
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.drag-handle {
  cursor: grab;
  color: var(--el-text-color-placeholder);
  flex-shrink: 0;
  padding: 4px;
  border-radius: 4px;
  transition:
    background-color 0.2s,
    color 0.2s;
}
.drag-handle:hover {
  background-color: var(--el-fill-color-light);
  color: var(--el-text-color-regular);
}
.drag-handle:active {
  cursor: grabbing;
}
.node-name-input {
  width: 180px;
}
.device-count-badge {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 10px;
  white-space: nowrap;
}
.device-card-header-right {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}
.node-remove {
  opacity: 0;
  transition: opacity 0.2s;
  flex-shrink: 0;
}
.device-card:hover .node-remove {
  opacity: 1;
}
.device-rows {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-left: 30px;
}
.device-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.device-select {
  flex: 1;
  min-width: 0;
}
.device-count-input {
  width: 110px;
  flex-shrink: 0;
}
.quick-add {
  display: flex;
  align-items: center;
  gap: 6px;
  padding-left: 30px;
  margin-top: 6px;
  flex-wrap: wrap;
}
.quick-add-label {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
  flex-shrink: 0;
}
.add-device-btn {
  flex-shrink: 0;
}
.add-node-btn {
  border-style: dashed;
}
.is-duplicate :deep(.el-input__wrapper),
.is-invalid :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--el-color-danger) inset;
}
.dup-tip {
  color: var(--el-color-danger);
  font-size: 12px;
  white-space: nowrap;
}
</style>
