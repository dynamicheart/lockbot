<template>
  <el-button aria-haspopup="dialog" @click="visible = true">已加载插件</el-button>
  <el-dialog
    v-model="visible"
    title="已加载插件"
    width="min(460px, calc(100vw - 32px))"
    append-to-body
  >
    <dl class="plugins">
      <div class="plugin">
        <dt>外部资源接入</dt>
        <dd>将其他 LockBot 的个人占用和预约汇总到当前工作台。</dd>
        <dd class="plugin-status">{{ reporterStatus }}</dd>
        <dd v-if="reporterNames.length" class="plugin-names">{{ reporterNames.join('、') }}</dd>
      </div>
      <div class="plugin">
        <dt>利用率数据源</dt>
        <dd>在已授权的资源明细里查看节点和设备的利用率。</dd>
        <dd class="plugin-status">{{ metricsStatus }}</dd>
        <dd v-if="metricsNames.length" class="plugin-names">{{ metricsNames.join('、') }}</dd>
      </div>
    </dl>
    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  inventory: { type: Object, default: null },
  groups: { type: Array, required: true },
  metricSources: { type: Array, required: true },
  loading: Boolean,
})
const visible = ref(false)
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
  if (Number.isSafeInteger(count) && count > 0) return `服务已加载 ${count} 个资源报表插件`
  if (count === 0) return '服务未加载资源报表插件'
  if (reporterNames.value.length) return '当前可见的资源来源'
  return props.loading ? '正在读取资源来源' : '本次未识别到外部资源来源'
})
const metricsStatus = computed(() =>
  props.metricSources.length
    ? '当前持有资源已配置数据源'
    : props.loading
      ? '正在读取资源来源'
      : '当前持有资源暂无可用数据源'
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
