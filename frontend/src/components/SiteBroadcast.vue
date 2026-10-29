<template>
  <el-card class="broadcast-card">
    <template #header>{{ $t('broadcast.title') }}</template>
    <el-form label-position="top" class="broadcast-compose">
      <el-form-item :label="$t('broadcast.message')">
        <el-input v-model="message" type="textarea" :rows="3" :maxlength="1800" show-word-limit />
      </el-form-item>
      <div class="options">
        <el-select v-model="days" style="width: 180px" :aria-label="$t('broadcast.window')">
          <el-option
            v-for="n in [1, 7, 30]"
            :key="n"
            :value="n"
            :label="$t('broadcast.days', { n })"
          />
        </el-select>
        <el-checkbox v-model="mentionAll">{{ $t('broadcast.mentionAll') }}</el-checkbox>
        <el-button type="primary" :disabled="!message.trim()" :loading="busy" @click="preview">
          {{ $t('broadcast.preview') }}
        </el-button>
      </div>
    </el-form>
    <el-dialog
      v-model="dialog"
      :title="$t(job?.status === 'draft' ? 'broadcast.preview' : 'broadcast.details')"
      width="min(640px, 95vw)"
      class="broadcast-dialog"
    >
      <template v-if="job">
        <div class="message-preview">{{ job.message }}</div>
        <div class="preview-summary">
          <span>{{ $t('broadcast.coverage', { n: job.total }) }}</span>
          <span>{{
            job.mention_all ? $t('broadcast.mentionAll') : $t('broadcast.noMention')
          }}</span>
          <span v-if="job.status !== 'draft'">{{ progress(job) }}</span>
        </div>
        <p v-if="!job.total" class="hint">{{ $t('broadcast.empty') }}</p>
        <el-table :data="job.targets" max-height="320">
          <el-table-column :label="$t('broadcast.bot')" min-width="230">
            <template #default="{ row }">
              <div class="target-info">
                <span class="target-name">{{ row.bot_name || groupLabel(row) }}</span>
                <span v-if="knownGroupName(row)" class="target-group-name">{{
                  knownGroupName(row)
                }}</span>
                <div class="target-id-row">
                  <el-tooltip :content="row.group_id" :disabled="!row.group_id">
                    <code class="group-id">{{ row.group_id || '—' }}</code>
                  </el-tooltip>
                  <el-button
                    v-if="row.group_id"
                    link
                    size="small"
                    @click="copyText(row.group_id)"
                    >{{ $t('common.copy') }}</el-button
                  >
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column
            v-if="job.status !== 'draft'"
            :label="$t('broadcast.result')"
            width="150"
          >
            <template #default="{ row }">
              <el-tag size="small" :type="statusType(row.status)" effect="plain">{{
                $t('broadcast.states.' + row.status)
              }}</el-tag>
              <p v-if="resultDetail(row)" class="result-detail">{{ resultDetail(row) }}</p>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          v-if="job.total > 100"
          v-model:current-page="page"
          :page-size="100"
          :total="job.total"
          layout="prev, pager, next"
          @current-change="refreshJob"
        />
      </template>
      <template #footer>
        <el-button @click="dialog = false">{{ $t('common.close') }}</el-button>
        <el-button
          v-if="job?.status === 'draft'"
          type="primary"
          :disabled="!job.total"
          :loading="busy"
          @click="send"
        >
          {{ $t('broadcast.confirmSend') }}
        </el-button>
      </template>
    </el-dialog>
    <div v-if="history.length" class="history">
      <h4>{{ $t('broadcast.history') }}</h4>
      <el-table :data="history" @row-click="openJob">
        <el-table-column
          prop="message"
          :label="$t('broadcast.message')"
          min-width="220"
          show-overflow-tooltip
        />
        <el-table-column :label="$t('broadcast.status')" width="120">
          <template #default="{ row }"
            ><el-tag size="small" :type="statusType(row.status)">{{
              $t('broadcast.states.' + row.status)
            }}</el-tag></template
          >
        </el-table-column>
        <el-table-column :label="$t('broadcast.progress')" width="260">
          <template #default="{ row }">{{ progress(row) }}</template>
        </el-table-column>
        <el-table-column width="64" align="right">
          <template #default="{ row }"
            ><el-button link type="primary" @click.stop="openJob(row)">{{
              $t('broadcast.view')
            }}</el-button></template
          >
        </el-table-column>
      </el-table>
    </div>
  </el-card>
</template>
<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
import { useHelpers } from '../utils/helpers'
const { t } = useI18n()
const { copyText } = useHelpers()
const knownGroupName = (row) => row.group_name || row.chat_name || row.cached_group_name || ''
const groupLabel = (row) => knownGroupName(row) || row.group_id || '—'
const resultDetail = (row) =>
  row.result_code && row.status !== 'accepted' && !['pending', 'sending'].includes(row.status)
    ? t('broadcast.results.' + row.result_code)
    : ''

const statusType = (status) =>
  ({
    accepted: 'success',
    failed: 'danger',
    unknown: 'warning',
  })[status] || 'info'
const message = ref('')
const days = ref(7)
const mentionAll = ref(false)
const busy = ref(false)
const dialog = ref(false)
const job = ref(null)
const page = ref(1)
const history = ref([])
let timer
let disposed = false
const progress = (row) =>
  t('broadcast.progressText', {
    done: row.total - (row.counts.pending || 0) - (row.counts.sending || 0),
    total: row.total,
    accepted: row.counts.accepted || 0,
  })
async function loadHistory() {
  history.value = (await api.get('/admin/broadcasts', { _silent: true })).data
}
async function refreshJob() {
  if (job.value)
    job.value = (
      await api.get(`/admin/broadcasts/${job.value.id}`, {
        params: { offset: (page.value - 1) * 100 },
      })
    ).data
}
async function openJob(row) {
  job.value = row
  page.value = 1
  await refreshJob()
  dialog.value = true
}
async function preview() {
  busy.value = true
  try {
    job.value = (
      await api.post('/admin/broadcasts/preview', {
        message: message.value,
        recent_days: days.value,
        mention_all: mentionAll.value,
      })
    ).data
    page.value = 1
    dialog.value = true
  } finally {
    busy.value = false
  }
}
async function send() {
  try {
    await ElMessageBox.confirm(
      t('broadcast.confirmText', {
        n: job.value.total,
        mention: job.value.mention_all ? t('broadcast.mentionAll') : t('broadcast.noMention'),
      }),
      t('broadcast.title'),
      {
        type: 'warning',
        confirmButtonText: t('broadcast.confirmSend'),
        cancelButtonText: t('common.cancel'),
      }
    )
  } catch {
    return
  }
  busy.value = true
  try {
    job.value = (await api.post(`/admin/broadcasts/${job.value.id}/send`)).data
    await loadHistory()
  } finally {
    busy.value = false
  }
}
async function poll() {
  try {
    await loadHistory()
    if (dialog.value && ['queued', 'running'].includes(job.value?.status)) await refreshJob()
  } catch {
    /* Keep the last known progress during a transient disconnection. */
  }
  if (!disposed) timer = setTimeout(poll, 3000)
}
onMounted(poll)
onUnmounted(() => {
  disposed = true
  clearTimeout(timer)
})
</script>
<style scoped>
.broadcast-card {
  margin-bottom: 20px;
}
.broadcast-compose {
  width: 100%;
}
.target-info {
  min-width: 0;
  display: grid;
  gap: 3px;
}
.target-name {
  font-weight: 500;
}
.preview-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  margin: 14px 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.result-detail {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}
.target-group-name {
  color: var(--el-text-color-secondary);
}
.target-id-row {
  display: flex;
  gap: 8px;
  align-items: center;
  min-width: 0;
}
.group-id {
  max-width: 260px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.target-id-row .el-button {
  flex-shrink: 0;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}
.options {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.message-preview {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  padding: 12px 14px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
}
.history h4 {
  margin: 0 0 12px;
  font-size: 14px;
  font-weight: 600;
}
.history {
  margin-top: 24px;
}
</style>
