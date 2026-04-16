<template>
  <div>
    <el-alert
      :title="$t('botForm.queueInfo')"
      type="info"
      :closable="false"
      show-icon
      style="margin-bottom: 16px"
    />
    <NodeBotForm
      :model-value="modelValue"
      @update:model-value="$emit('update:modelValue', $event)"
    />
  </div>
</template>

<script setup>
import NodeBotForm from './NodeBotForm.vue'

defineProps({
  modelValue: { type: Object, default: () => ({}) },
})
defineEmits(['update:modelValue'])
</script>
