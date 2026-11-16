<template>
  <el-button v-if="showTrigger" aria-haspopup="dialog" @click="visible = true">
    {{ t('personalWorkbench.plugins.title') }}
  </el-button>
  <el-dialog
    v-model="visible"
    :title="t('personalWorkbench.plugins.title')"
    width="min(460px, calc(100vw - 32px))"
    append-to-body
  >
    <dl class="plugins">
      <div class="plugin">
        <dt>{{ t('personalWorkbench.plugins.reporterTitle') }}</dt>
        <dd>{{ t('personalWorkbench.plugins.reporterDescription') }}</dd>
        <dd class="plugin-status">{{ reporterStatus }}</dd>
        <dd v-if="reporterNames.length" class="plugin-names">
          {{ namesFormat.format(reporterNames) }}
        </dd>
      </div>
      <div class="plugin">
        <dt>{{ t('personalWorkbench.plugins.metricsTitle') }}</dt>
        <dd>{{ t('personalWorkbench.plugins.metricsDescription') }}</dd>
        <dd class="plugin-status">{{ metricsStatus }}</dd>
        <dd v-if="metricsNames.length" class="plugin-names">
          {{ namesFormat.format(metricsNames) }}
        </dd>
      </div>
    </dl>
    <template #footer>
      <el-button @click="visible = false">{{ t('common.close') }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

const { t, locale } = useI18n()
const props = defineProps({
  showTrigger: { type: Boolean, default: true },
  inventory: { type: Object, default: null },
  groups: { type: Array, required: true },
  metricSources: { type: Array, required: true },
  loading: Boolean,
})
const visible = ref(false)
const namesFormat = computed(() => new Intl.ListFormat(locale.value))
defineExpose({ open: () => (visible.value = true) })
const uniqueNames = (names) => [
  ...new Set(names.filter((name) => typeof name === 'string' && name.trim())),
]
const reporterNames = computed(() =>
  uniqueNames(
    props.groups
      .filter((group) => group.source?.kind === 'reporter')
      .map((group) => group.source.label)
  )
)
const metricsNames = computed(() =>
  uniqueNames(props.metricSources.map((source) => source.configuration.name))
)
const reporterStatus = computed(() => {
  const count = props.inventory?.resource_reporters
  if (Number.isSafeInteger(count) && count > 0)
    return t('personalWorkbench.plugins.reporterLoaded', count)
  if (count === 0) return t('personalWorkbench.plugins.reporterNotLoaded')
  if (reporterNames.value.length) return t('personalWorkbench.plugins.reporterVisible')
  return t(
    props.loading
      ? 'personalWorkbench.plugins.loadingSources'
      : 'personalWorkbench.plugins.reporterUnknown'
  )
})
const metricsStatus = computed(() =>
  props.metricSources.length
    ? t('personalWorkbench.plugins.metricsConfigured')
    : props.loading
      ? t('personalWorkbench.plugins.loadingSources')
      : t('personalWorkbench.plugins.metricsUnavailable')
)
</script>

<style scoped>
.plugins {
  display: grid;
  gap: 16px;
  margin: 0;
}
.plugin {
  display: grid;
  gap: 4px;
  overflow-wrap: anywhere;
}
.plugin + .plugin {
  padding-top: 16px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.plugin dt {
  color: var(--el-text-color-primary);
  font-weight: 600;
}
.plugin dd {
  margin: 0;
}
.plugin-status {
  color: var(--el-text-color-secondary);
}
.plugin-names {
  color: var(--el-text-color-primary);
}
</style>
