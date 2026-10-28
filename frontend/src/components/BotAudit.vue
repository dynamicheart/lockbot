<template>
  <div v-loading="loading">
    <div style="display: flex; justify-content: flex-end; margin-bottom: 12px">
      <el-button size="small" @click="load">{{ $t('common.refresh') }}</el-button>
    </div>
    <el-table :data="items" :empty-text="$t('audit.noData')">
      <el-table-column :label="$t('audit.time')" width="180">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column prop="operator_username" :label="$t('audit.operator')" min-width="180" />
      <el-table-column :label="$t('audit.action')" min-width="160">
        <template #default="{ row }">{{ $tm('audit.actions')[row.action] || row.action }}</template>
      </el-table-column>
      <el-table-column :label="$t('audit.result')" width="100">
        <template #default="{ row }">{{ $t(`audit.${row.result}`) }}</template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-if="total > 20"
      v-model:current-page="page"
      :page-size="20"
      :total="total"
      layout="prev, pager, next"
      style="margin-top: 12px"
      @current-change="load"
    />
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
import { useHelpers } from '../utils/helpers'
const props = defineProps({ botId: { type: Number, required: true } })
const { t } = useI18n()
const { formatDateTime } = useHelpers()
const items = ref([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
let revision = 0
async function load() {
  const current = ++revision
  loading.value = true
  try {
    const { data } = await api.get(`/bots/${props.botId}/audit-logs`, {
      params: { page: page.value, limit: 20 },
    })
    if (revision === current) {
      items.value = data.items
      total.value = data.total
    }
  } catch {
    if (revision === current) ElMessage.error(t('agentActivity.error'))
  } finally {
    if (revision === current) loading.value = false
  }
}
watch(
  () => props.botId,
  () => {
    items.value = []
    total.value = 0
    page.value = 1
    load()
  },
  { immediate: true }
)
</script>
