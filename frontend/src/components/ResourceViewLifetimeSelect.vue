<template>
  <el-select
    :model-value="modelValue"
    :aria-label="$t('resourceView.linkLifetime')"
    class="resource-link-lifetime"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <el-option v-if="allowInherit" value="inherit" :label="$t('resourceView.inheritLifetime')" />
    <el-option :value="0" :label="$t('resourceView.permanent')" />
    <el-option
      v-for="days in 30"
      :key="days"
      :value="days"
      :label="$t('resourceView.lifetimeDays', { count: days })"
    />
  </el-select>
</template>

<script setup>
defineProps({
  modelValue: { type: [Number, String], required: true },
  allowInherit: Boolean,
})
defineEmits(['update:modelValue'])
</script>

<style scoped>
.resource-link-lifetime {
  width: 220px;
  max-width: 100%;
}
</style>
