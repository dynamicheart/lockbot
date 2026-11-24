<template>
  <el-button
    v-if="showTrigger"
    class="workbench-extension"
    :icon="Grid"
    text
    aria-haspopup="dialog"
    @click="visible = true"
  >
    {{ t('personalWorkbench.plugins.title') }}
  </el-button>
  <el-dialog
    v-model="visible"
    :title="t('personalWorkbench.plugins.title')"
    width="min(460px, calc(100vw - 32px))"
    append-to-body
    align-center
  >
    <div class="plugins">
      <section class="plugin">
        <div class="plugin-heading">
          <h3>{{ t('personalWorkbench.plugins.reporterTitle') }}</h3>
          <span class="plugin-state" :class="{ available: reporterAvailable }">{{
            reporterStatus
          }}</span>
        </div>
        <p>{{ t('personalWorkbench.plugins.reporterDescription') }}</p>
        <p v-if="reporterNames.length" class="plugin-names">
          {{ namesFormat.format(reporterNames) }}
        </p>
      </section>
      <section class="plugin">
        <div class="plugin-heading">
          <h3>{{ t('personalWorkbench.plugins.metricsTitle') }}</h3>
          <span class="plugin-state" :class="{ available: metricSources.length }">{{
            metricsStatus
          }}</span>
        </div>
        <p>{{ t('personalWorkbench.plugins.metricsDescription') }}</p>
        <p v-if="metricsNames.length" class="plugin-names">
          {{ namesFormat.format(metricsNames) }}
        </p>
      </section>
    </div>
    <template #footer>
      <el-button @click="visible = false">{{ t('common.close') }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Grid } from '@element-plus/icons-vue'

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
const reporterAvailable = computed(
  () => props.inventory?.resource_reporters > 0 || reporterNames.value.length > 0
)
const reporterStatus = computed(() => {
  const count = props.inventory?.resource_reporters
  if (Number.isSafeInteger(count) && count > 0) return t('personalWorkbench.plugins.reporterLoaded')
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
  gap: 12px;
  margin: 0;
}
.plugin {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 16px;
  overflow-wrap: anywhere;
}
.plugin-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}
.plugin h3 {
  margin: 0;
  font-size: 15px;
  color: var(--el-text-color-primary);
  font-weight: 600;
}
.plugin p {
  margin: 8px 0 0;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}
.plugin-state {
  padding: 2px 7px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.plugin-state.available {
  background: var(--el-color-success-light-9);
  color: var(--el-color-success-dark-2);
}
.plugin-names {
  color: var(--el-text-color-primary);
}
</style>
