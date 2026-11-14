<template>
  <el-select
    :model-value="modelValue"
    :aria-label="$t('resourceView.linkLifetime')"
    class="resource-link-lifetime"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <el-option v-if="allowInherit" value="inherit" :label="$t('resourceView.inheritLifetime')" />
    <el-option
      v-if="typeof modelValue === 'number' && !presets.some(({ days }) => days === modelValue)"
      :value="modelValue"
      :label="$t('resourceView.lifetimeDays', { count: modelValue })"
    />
    <el-option
      v-for="{ days, label } in presets"
      :key="days"
      :value="days"
      :label="$t(label)"
    />
  </el-select>
</template>

<script setup>
const presets = [
  { days: 1, label: 'settings.lifetimeOneDay' },
  { days: 7, label: 'settings.lifetimeOneWeek' },
  { days: 30, label: 'settings.lifetimeOneMonth' },
  { days: 90, label: 'settings.lifetimeThreeMonths' },
  { days: 180, label: 'settings.lifetimeHalfYear' },
  { days: 0, label: 'resourceView.permanent' },
]

defineProps({
  modelValue: { type: [Number, String], required: true },
  allowInherit: Boolean,
})
defineEmits(['update:modelValue'])
</script>

<style scoped>
.resource-link-lifetime {
  width: 220px;
  flex-shrink: 0;
  max-width: 100%;
}
</style>
