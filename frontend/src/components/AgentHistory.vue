<template>
  <section class="agent-history">
    <div class="history-toolbar">
      <el-input
        v-model="query"
        clearable
        :placeholder="$t('agentHistory.search')"
        @keyup.enter="reset"
        @clear="reset"
      />
      <el-button :loading="loading" size="small" @click="reset">{{
        $t('common.refresh')
      }}</el-button>
      <el-tooltip :content="$t('agentHistory.retention')"><span tabindex="0">ⓘ</span></el-tooltip>
    </div>
    <p v-if="restricted" class="privacy-note">{{ $t('log.privateDetailsRestricted') }}</p>
    <el-alert v-if="error" :title="$t('agentHistory.error')" type="warning" :closable="false" />
    <el-table v-loading="loading" :data="items" row-key="id" :empty-text="$t('common.noData')">
      <el-table-column type="expand">
        <template #default="{ row }">
          <pre class="event-detail">{{ JSON.stringify(row, null, 2) }}</pre>
        </template>
      </el-table-column>
      <el-table-column :label="$t('agentHistory.time')" min-width="170"
        ><template #default="{ row }">{{ time(row.timestamp) }}</template></el-table-column
      >
      <el-table-column :label="$t('agentHistory.user')" min-width="150"
        ><template #default="{ row }"
          >{{ row.actor?.external_user_id || '—'
          }}<small
            >{{ row.actor?.platform }}
            <span v-if="row.credential_ref || row.credential_id">
              · {{ row.credential_ref || '#' + row.credential_id }}</span
            ></small
          ></template
        ></el-table-column
      >
      <el-table-column :label="$t('agentHistory.operation')" min-width="180" show-overflow-tooltip
        ><template #default="{ row }">{{
          row.command || $t('agentHistory.operations.' + row.operation)
        }}</template></el-table-column
      >
      <el-table-column :label="$t('agentHistory.resource')" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ targets(row.requested_resources) }}</template>
      </el-table-column>
      <el-table-column :label="$t('agentHistory.task')" min-width="220" show-overflow-tooltip
        ><template #default="{ row }"
          >{{ row.public_task || '—'
          }}<small v-if="row.agent_label || row.purpose"
            >{{ row.agent_label }} {{ row.purpose }}</small
          ></template
        ></el-table-column
      >
      <el-table-column :label="$t('agentHistory.outcome')" min-width="150"
        ><template #default="{ row }"
          >{{ $t('agentHistory.outcomes.' + row.outcome)
          }}<small>HTTP {{ row.http_status ?? '—' }}</small></template
        ></el-table-column
      >
    </el-table>
    <div class="history-pages">
      <el-button size="small" :disabled="page === 0 || loading" @click="previous">{{
        $t('agentHistory.previous')
      }}</el-button>
      <span>{{ page + 1 }}</span>
      <el-button size="small" :disabled="!nextCursor || loading" @click="next">{{
        $t('agentHistory.next')
      }}</el-button>
    </div>
  </section>
</template>
<script setup>
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import api from '../utils/api'
const props = defineProps({
  botId: { type: [Number, String], required: true },
  includePrivate: { type: Boolean, default: false },
})
const { locale } = useI18n()
const query = ref('')
const activeQuery = ref('')
const items = ref([])
const restricted = ref(false)
const error = ref(false)
const loading = ref(false)
const page = ref(0)
const cursors = [null]
const nextCursor = ref(null)
let generation = 0
let controller = null
const targets = (values) =>
  (Array.isArray(values) ? values : [])
    .map((entry) => {
      if (typeof entry === 'string') return entry
      if (!entry || typeof entry !== 'object') return '—'
      return (
        (entry.resource_id || '—') +
        (Array.isArray(entry.device_ids) ? ` dev${entry.device_ids.join(',')}` : '')
      )
    })
    .join(', ') || '—'
const time = (value) => (value ? new Date(value).toLocaleString(locale.value) : '—')
async function load() {
  controller?.abort()
  controller = new AbortController()
  const current = ++generation
  loading.value = true
  error.value = false
  try {
    const { data } = await api.get(`/bots/${props.botId}/agent-history`, {
      params: {
        cursor: cursors[page.value] || undefined,
        query: activeQuery.value,
        limit: 25,
        include_private: props.includePrivate,
      },
      signal: controller.signal,
    })
    if (current === generation) {
      restricted.value = !!data.details_restricted
      items.value = data.items
      nextCursor.value = data.next_cursor
    }
  } catch (e) {
    if (current === generation && e.code !== 'ERR_CANCELED') {
      error.value = true
      items.value = []
      nextCursor.value = null
    }
  } finally {
    if (current === generation) loading.value = false
  }
}
function reset() {
  activeQuery.value = query.value
  cursors.splice(0, cursors.length, null)
  page.value = 0
  items.value = []
  load()
}
function next() {
  cursors[page.value + 1] = nextCursor.value
  page.value++
  load()
}
function previous() {
  page.value--
  load()
}
watch(
  [() => props.botId, () => props.includePrivate],
  () => {
    query.value = ''
    reset()
  },
  { flush: 'sync' }
)
onMounted(load)
onBeforeUnmount(() => {
  generation++
  controller?.abort()
})
</script>
<style scoped>
.privacy-note {
  color: var(--lb-text-secondary);
  font-size: 12px;
}
.history-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 12px 0;
}
.history-toolbar .el-input {
  max-width: 320px;
}
.history-pages {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}
small {
  display: block;
  color: var(--lb-text-secondary);
  font-size: 12px;
}
.event-detail {
  margin: 12px 24px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 320px;
  overflow: auto;
}
</style>
