<template>
  <div class="agent-traces">
    <iframe ref="viewer" :src="viewerUrl" title="Agent 轨迹" @load="synchronize" />
    <el-drawer v-model="reviewOpen" title="评审评论" :size="drawerWidth" :close-on-click-modal="false">
      <template v-if="draftContext">
        <p class="review-context">{{ draftContext.case_name }} · {{ isUiComment(draftContext) ? '界面点评' : draftContext.scope === 'case' ? '测例点评' : draftContext.model }}</p>
        <p v-if="draftContext.anchor !== 'run'" class="review-field">{{ draftContext.field_label }}</p>
        <blockquote v-if="draftContext.quote">{{ draftContext.quote }}</blockquote>
        <el-select v-if="draftContext.scope === 'case'" v-model="reviewSubject" :disabled="saving" aria-label="点评内容类别" class="review-subject">
          <el-option label="整体" value="general" />
          <el-option label="使用场景" value="scenario" />
          <el-option label="用户提示词" value="prompt" />
          <el-option label="预期行为" value="expected" />
          <el-option label="判定标准" value="grading" />
          <el-option label="界面与文案" value="interface" />
        </el-select>
        <el-input
          v-model="draft"
          :disabled="saving"
          type="textarea"
          :rows="4"
          maxlength="4000"
          show-word-limit
          placeholder="这里哪里不合适？你希望怎么改？"
          aria-label="评论内容"
        />
        <el-alert v-if="saveError" :title="saveError" type="error" :closable="false" show-icon />
        <el-alert v-if="saved" title="评论已保存到测试服务" type="success" :closable="false" show-icon />
        <p v-if="!draftContext.server_backed" class="review-context">
          本地文件的评论仅保留在此浏览器草稿中，可导出交给我 review。
        </p>
        <div class="review-actions">
          <el-button type="primary" :loading="saving" :disabled="!draft.trim() || !draftContext.server_backed" @click="save">
            保存评论
          </el-button>
          <el-button :disabled="!draft.trim() || saving" @click="exportDraft">导出评论</el-button>
          <el-button :disabled="!draft.trim() || saving" @click="clearDraft">清空草稿</el-button>
        </div>
      </template>
      <el-divider />
      <el-alert v-if="loadError" :title="loadError" type="error" :closable="false" show-icon />
      <div v-if="caseComments.length" class="review-list-heading">测例点评 · 所有模型共用</div>
      <article v-for="comment in caseComments" :key="comment.id" class="review-comment">
        <p class="review-field">{{ subjectLabels[comment.subject] || '整体' }}</p>
        <div class="review-comment-meta"><span>{{ comment.author }} · {{ localTime(comment.created_at) }}</span></div>
        <blockquote v-if="comment.quote">{{ comment.quote }}</blockquote>
        <p>{{ comment.body }}</p>
        <el-button link @click="resolve(comment)">{{ comment.status === 'resolved' ? '重新打开' : '标记已处理' }}</el-button>
      </article>
      <div v-if="uiComments.length" class="review-list-heading">界面点评 · 所有批次共用</div>
      <article v-for="comment in uiComments" :key="comment.id" class="review-comment">
        <div class="review-comment-meta"><span>{{ comment.author }} · {{ localTime(comment.created_at) }}</span></div>
        <blockquote v-if="comment.quote">{{ comment.quote }}</blockquote>
        <p>{{ comment.body }}</p>
        <el-button link type="primary" @click="locate(comment)">定位界面</el-button>
        <el-button link @click="resolve(comment)">{{ comment.status === 'resolved' ? '重新打开' : '标记已处理' }}</el-button>
      </article>
      <div class="review-list-heading">
        <span>本次记录</span>
        <el-checkbox v-model="showResolved">包括已处理</el-checkbox>
      </div>
      <el-empty v-if="!currentComments.length" description="还没有评论" :image-size="48" />
      <article v-for="comment in currentComments" :key="comment.id" class="review-comment">
        <div class="review-comment-meta">
          <span>{{ comment.author }} · {{ localTime(comment.created_at) }}</span>
          <el-tag v-if="comment.status === 'resolved'" size="small" type="success">已处理</el-tag>
        </div>
        <blockquote v-if="comment.quote">{{ comment.quote }}</blockquote>
        <p>{{ comment.body }}</p>
        <el-button link type="primary" @click="locate(comment)">定位原文</el-button>
        <el-button link @click="resolve(comment)">
          {{ comment.status === 'resolved' ? '重新打开' : '标记已处理' }}
        </el-button>
      </article>
      <el-collapse v-if="historicalComments.length" class="review-history">
        <el-collapse-item title="此测例的其他记录与历史评论" name="history">
          <article v-for="comment in historicalComments" :key="comment.id" class="review-comment">
            <p class="review-context">{{ comment.model }} · 较早记录</p>
            <blockquote v-if="comment.quote">{{ comment.quote }}</blockquote>
            <p>{{ comment.body }}</p>
          </article>
        </el-collapse-item>
      </el-collapse>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '../utils/api'

const route = useRoute()
const router = useRouter()
const viewer = ref(null)
const reviewOpen = ref(false)
const current = ref(null)
const draftContext = ref(null)
const draft = ref('')
const comments = ref([])
const saving = ref(false)
const saved = ref(false)
const saveError = ref('')
const loadError = ref('')
const showResolved = ref(false)
const reviewSubject = ref('general')
const drawerWidth = 'min(440px, 100vw)'
const draftKey = 'lockbot_trace_review_draft'
let requestGeneration = 0
let themeObserver
let requestedRoute = ''
const subjectLabels = { general: '整体', scenario: '使用场景', prompt: '用户提示词', expected: '预期行为', grading: '判定标准', interface: '界面与文案' }

function routeFields(query) {
  const fields = {}
  if (typeof query.case === 'string' && query.case.length <= 256) fields.case = query.case
  for (const key of ['run', 'step']) {
    if (/^\d{1,5}$/.test(String(query[key] ?? ''))) fields[key] = String(query[key])
  }
  if (query.view === 'steps') fields.view = 'steps'
  if (typeof query.dataset_id === 'string' && /^[a-f0-9]{64}$/.test(query.dataset_id)) fields.dataset_id = query.dataset_id
  return fields
}
const viewerUrl = ref('/dev/trace-review?raw=true&embedded=true#' + new URLSearchParams(routeFields(route.query)))
function send(message) {
  viewer.value?.contentWindow?.postMessage(message, location.origin)
}
function synchronize() {
  send({ type: 'lockbot-trace-theme', dark: document.documentElement.classList.contains('dark') })
  send({ type: 'lockbot-trace-review-open', open: reviewOpen.value })
  send({ type: 'lockbot-trace-set-route', route: routeFields(route.query) })
}
function validContext(value) {
  return value && typeof value.case_id === 'string' && value.case_id.length <= 256 &&
    (value.scope === 'case' ? value.run_index === null : Number.isInteger(value.run_index) && value.run_index >= 0 && value.run_index <= 10000) &&
    (value.run_id === null || /^[a-f0-9]{64}$/.test(value.run_id)) &&
    typeof value.case_name === 'string' && value.case_name.length <= 512 &&
    typeof value.model === 'string' && value.model.length <= 512 &&
    (value.dataset_id == null || (typeof value.dataset_id === 'string' && /^[a-f0-9]{64}$/.test(value.dataset_id)))
}
async function loadComments(context) {
  const generation = ++requestGeneration
  loadError.value = ''
  if (!context?.server_backed) {
    comments.value = []
    return
  }
  try {
    const response = await api.get('/dev/trace-review/comments', {
      params: { case_id: context.case_id, run_index: context.run_index, raw: true, dataset_id: context.dataset_id },
      _silent: true,
    })
    if (generation === requestGeneration) comments.value = response.data.comments
  } catch (error) {
    if (generation !== requestGeneration) return
    comments.value = []
    loadError.value = error.response?.status === 403 ? '仅超级管理员可查看评论。' : '评论未加载，请稍后重试。'
  }
}
function receive(event) {
  if (event.origin !== location.origin || event.source !== viewer.value?.contentWindow) return
  const message = event.data
  if (!message || typeof message !== 'object') return
  if (message.type === 'lockbot-trace-select-dataset' && message.route && typeof message.route === 'object') {
    if (message.route.dataset_id != null && (typeof message.route.dataset_id !== 'string' || !/^[a-f0-9]{64}$/.test(message.route.dataset_id))) return
    router.push({ path: '/agent-traces', query: routeFields(message.route) })
  } else if (message.type === 'lockbot-trace-route' && validContext(message.context)) {
    const previous = current.value
    current.value = message.context
    const next = routeFields(message.route || {})
    const serialized = JSON.stringify(next)
    if (serialized !== JSON.stringify(routeFields(route.query)) && requestedRoute !== serialized) {
      requestedRoute = serialized
      router[message.navigation === 'push' ? 'push' : 'replace']({ path: '/agent-traces', query: next }).finally(() => { if (requestedRoute === serialized) requestedRoute = '' })
    }
    if (reviewOpen.value && (previous?.run_id !== current.value.run_id || previous?.dataset_id !== current.value.dataset_id) && !draft.value) {
      loadComments(current.value)
    }
  } else if (message.type === 'lockbot-trace-comment' && validContext(message.context)) {
    if (typeof message.anchor !== 'string' || message.anchor.length > 160 ||
        typeof message.quote !== 'string' || message.quote.length > 4000) return
    if (draft.value.trim() && (draftContext.value?.dataset_id !== message.context.dataset_id || draftContext.value?.case_id !== message.context.case_id || draftContext.value?.scope !== message.context.scope || draftContext.value?.run_id !== message.context.run_id || draftContext.value?.anchor !== message.anchor || draftContext.value?.quote !== message.quote)) {
      reviewOpen.value = true
      saveError.value = '上次的评论草稿还在，请先保存，或导出后清空草稿。'
      return
    }
    if (draft.value.trim()) { reviewOpen.value = true; return }
    draftContext.value = { ...message.context, anchor: message.anchor, quote: message.quote,
      field_label: typeof message.field_label === 'string' ? message.field_label.slice(0, 160) : '' }
    if (message.context.scope !== 'case' || current.value?.case_id !== message.context.case_id) current.value = message.context
    reviewSubject.value = ['general', 'scenario', 'prompt', 'expected', 'grading', 'interface'].includes(message.subject) ? message.subject : 'general'
    saved.value = false
    saveError.value = ''
    reviewOpen.value = true
    loadComments(message.context)
  }
}
function isUiComment(comment) {
  return typeof comment.anchor === 'string' && comment.anchor.startsWith('ui:')
}
function inCurrentDataset(comment) {
  return (comment.dataset_id ?? null) === (current.value?.dataset_id ?? null)
}
const uiComments = computed(() => comments.value.filter(comment => isUiComment(comment) &&
  comment.case_id === current.value?.case_id && (showResolved.value || comment.status !== 'resolved')))
const caseComments = computed(() => comments.value.filter(comment => !isUiComment(comment) && comment.scope === 'case' &&
  comment.case_id === current.value?.case_id && (showResolved.value || comment.status !== 'resolved')))
const currentComments = computed(() => comments.value.filter(comment =>
  !isUiComment(comment) && inCurrentDataset(comment) && comment.scope !== 'case' && comment.raw && comment.run_id === current.value?.run_id &&
  (showResolved.value || comment.status !== 'resolved')))
const historicalComments = computed(() => comments.value.filter(comment =>
  !isUiComment(comment) && inCurrentDataset(comment) && comment.scope !== 'case' && comment.raw && comment.case_id === current.value?.case_id && comment.run_id !== current.value?.run_id))
async function save() {
  const context = draftContext.value
  if (!context?.server_backed || !draft.value.trim()) return
  saving.value = true
  saved.value = false
  saveError.value = ''
  try {
    await api.post('/dev/trace-review/comments', {
      scope: context.scope || 'run', subject: reviewSubject.value,
      case_id: context.case_id, run_index: context.run_index, run_id: context.run_id,
      dataset_id: context.dataset_id, raw: true, anchor: context.anchor, quote: context.quote, body: draft.value,
    }, { _silent: true })
    draft.value = ''
    saved.value = true
    await loadComments(current.value)
  } catch (error) {
    saveError.value = error.response?.status === 409
      ? '轨迹或评论存储状态已变化，草稿已保留。请刷新记录后重试。'
      : '保存失败，草稿已保留，可以重试。'
  } finally {
    saving.value = false
  }
}
async function resolve(comment) {
  try {
    await api.patch('/dev/trace-review/comments/' + comment.id,
      { status: comment.status === 'resolved' ? 'open' : 'resolved' },
      { params: { dataset_id: comment.dataset_id }, _silent: true })
    await loadComments(current.value)
  } catch {
    loadError.value = '未能更新评论状态，请重试。'
  }
}
function locate(comment) {
  reviewOpen.value = false
  send(isUiComment(comment)
    ? { type: 'lockbot-trace-locate', anchor: comment.anchor }
    : { type: 'lockbot-trace-locate', run_id: comment.run_id, dataset_id: comment.dataset_id, anchor: comment.anchor })
}
function clearDraft() { draft.value = ''; saved.value = false; saveError.value = '' }
watch(draft, value => { if (value) saved.value = false })
function localTime(value) {
  return new Date(value).toLocaleString()
}
function exportDraft() {
  const url = URL.createObjectURL(new Blob([JSON.stringify({ ...draftContext.value, subject: reviewSubject.value, body: draft.value }, null, 2)],
    { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'lockbot-review-comment.json'
  link.click()
  URL.revokeObjectURL(url)
}
watch(() => route.query, synchronize)
watch(reviewOpen, value => send({ type: 'lockbot-trace-review-open', open: value }))
watch([draft, draftContext, reviewSubject], () => {
  try {
    if (draft.value) localStorage.setItem(draftKey, JSON.stringify({ context: draftContext.value, subject: reviewSubject.value, body: draft.value }))
    else localStorage.removeItem(draftKey)
  } catch {
    // A failed browser draft write must not claim server persistence or discard the input.
  }
}, { deep: true })
onMounted(() => {
  window.addEventListener('message', receive)
  themeObserver = new MutationObserver(synchronize)
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
  try {
    const pending = JSON.parse(sessionStorage.getItem('lockbot_trace_pending_comment') || 'null')
    if (pending?.context && validContext(pending.context)) {
      receive({ origin: location.origin, source: viewer.value.contentWindow, data: pending })
      sessionStorage.removeItem('lockbot_trace_pending_comment')
    }
    const stored = JSON.parse(localStorage.getItem(draftKey) || 'null')
    if (stored && validContext(stored.context) && typeof stored.body === 'string') {
      draftContext.value = stored.context
      current.value = stored.context
      if (['general', 'scenario', 'prompt', 'expected', 'grading', 'interface'].includes(stored.subject)) reviewSubject.value = stored.subject
      loadComments(stored.context)
      draft.value = stored.body.slice(0, 4000)
      reviewOpen.value = true
    }
  } catch {
    // Invalid drafts do not affect loading the current trace.
  }
})
onBeforeUnmount(() => {
  window.removeEventListener('message', receive)
  themeObserver?.disconnect()
})
</script>

<style scoped>
.agent-traces, iframe { width: 100%; height: 100dvh; border: 0; display: block; }
.review-context, .review-field, .review-comment-meta { color: var(--lb-text-regular); font-size: 12px; }
.review-context, .review-field { margin-bottom: 10px; }
.review-subject { width: 100%; margin-bottom: 12px; }
blockquote { margin: 10px 0 14px; padding: 10px 12px; border-left: 3px solid var(--lb-color-primary); background: var(--lb-bg-code); white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; }
.review-actions { display: flex; gap: 8px; margin-top: 14px; }
.review-list-heading, .review-comment-meta { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.review-list-heading { font-size: 14px; font-weight: 600; }
.review-comment { padding: 14px 0; border-bottom: 1px solid var(--lb-border-color); }
.review-comment > p { white-space: pre-wrap; overflow-wrap: anywhere; margin-bottom: 8px; }
.review-comment-meta { margin-bottom: 8px; }
.review-comment blockquote { margin-block: 8px; }
.review-history { margin-top: 20px; }
.el-alert { margin-top: 12px; }
</style>
