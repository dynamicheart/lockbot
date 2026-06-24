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
      :aliases="aliases"
      @update:model-value="$emit('update:modelValue', $event)"
      @update:has-error="$emit('update:hasError', $event)"
      @update:aliases="$emit('update:aliases', $event)"
    />
  </div>
</template>

<script setup>
import NodeBotForm from './NodeBotForm.vue'

defineProps({
  modelValue: { type: Object, default: () => ({}) },
  aliases: { type: Object, default: () => ({}) },
})
defineEmits(['update:modelValue', 'update:hasError', 'update:aliases'])
</script>
