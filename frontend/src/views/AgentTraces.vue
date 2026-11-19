<template>
  <div class="agent-traces">
    <div v-if="routeFields(route.query).dataset_id || routeFields(route.query).legacy" class="review-history-banner"><span>较早记录</span><el-button link type="primary" @click="returnCurrent">返回当前记录</el-button></div>
    <div v-if="reselecting" class="review-history-banner"><span>正文已保留，请重新选原文并点击评论。</span><el-button link @click="cancelReselect">返回草稿</el-button></div>
    <iframe :key="viewerRevision" ref="viewer" :src="viewerUrl" title="Agent 轨迹" @load="synchronize" />
    <el-drawer v-model="reviewOpen" title="评论" :size="drawerWidth">
      <template v-if="draftContext">
        <p v-if="draft.trim() && !sameTarget(draftContext, current)" class="review-context">草稿仍属于原记录；保存或清空后可评论当前记录。</p>
        <p class="review-context">{{ draftContext.case_name }} · {{ isUiComment(draftContext) ? '界面评论' : draftContext.scope === 'case' ? '测例评论' : displayModelName(draftContext.model) }}</p>
        <p v-if="draftContext.field_label" class="review-field">{{ draftContext.field_label }}</p>
        <blockquote v-if="draftContext.quote">{{ draftContext.quote }}</blockquote>
        <el-select v-if="draftContext.scope === 'case'" v-model="reviewSubject" :disabled="saving" aria-label="评论内容类别" class="review-subject">
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
        <el-button v-if="saveErrorCode === 'trace_changed'" link @click="reselectOriginal">保留正文，重新选择原文</el-button>
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
      <div v-if="caseComments.length" class="review-list-heading">测例评论 · 所有模型共用</div>
      <article v-for="comment in caseComments" :key="comment.id" class="review-comment">
        <p class="review-field">{{ subjectLabels[comment.subject] || '整体' }}</p>
        <div class="review-comment-meta"><span>{{ comment.author }} · {{ localTime(comment.created_at) }}</span></div>
        <blockquote v-if="comment.quote">{{ comment.quote }}</blockquote>
        <p>{{ comment.body }}</p>
        <el-button link @click="resolve(comment)">{{ comment.status === 'resolved' ? '重新打开' : '标记已处理' }}</el-button>
      </article>
      <div v-if="uiComments.length" class="review-list-heading">界面评论 · 所有批次共用</div>
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
        <p v-if="comment.anchor === 'run' && comment.quote" class="review-field">本次记录的选文（可能跨段）</p>
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
            <p class="review-context">{{ displayModelName(comment.model) }} · {{ localTime(comment.created_at) }}</p>
            <p v-if="comment.anchor === 'run' && comment.quote" class="review-field">本次记录的选文（可能跨段）</p>
            <blockquote v-if="comment.quote">{{ comment.quote }}</blockquote>
            <p>{{ comment.body }}</p>
            <el-button v-if="comment.source_available" link type="primary" @click="locate(comment)">查看当时记录</el-button>
            <p v-else class="review-field">原记录暂不可用，评论仍保留。</p>
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
import { useAuthStore } from '../stores/auth'
import { LS_KEYS } from '../utils/demoMode'
import { displayModelName } from '../features/agent-traces/model-labels'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const viewer = ref(null)
const viewerRevision = ref(0)
const reviewOpen = ref(false)
const current = ref(null)
const draftContext = ref(null)
const draft = ref('')
const comments = ref([])
const saving = ref(false)
const saved = ref(false)
const saveError = ref('')
const saveErrorCode = ref('')
const loadError = ref('')
const showResolved = ref(false)
const reviewSubject = ref('general')
const drawerWidth = 'min(440px, 100vw)'
// sessionStorage is independent per tab, including duplicated tabs, and survives reload.
let tabId = globalThis.crypto?.randomUUID?.() || Date.now().toString(36) + '-' + Math.random().toString(36).slice(2)
try {
  tabId = sessionStorage.getItem('lockbot_trace_review_tab') || tabId
  sessionStorage.setItem('lockbot_trace_review_tab', tabId)
} catch { /* Draft input also works when browser storage is disabled. */ }
let draftOwner = ''
let identity = ''
let verifiedToken = auth.token
let verifiedUser = auth.user
let restoringDraft = false
let pendingLocate = null
const reselecting = ref(false)
const displayContext = computed(() => draftContext.value || current.value)
const draftKey = () => draftOwner ? 'lockbot_trace_review_draft:' + encodeURIComponent(draftOwner) + ':' + tabId : null
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
  if (query.legacy === '1') fields.legacy = '1'
  if (typeof query.review_run === 'string' && /^[a-f0-9]{64}$/.test(query.review_run)) fields.review_run = query.review_run
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
  // Do not echo an older router transition over a newer selection in the iframe.
  if (!requestedRoute) send({ type: 'lockbot-trace-set-route', route: routeFields(route.query) })
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
  const requester = authIdentity()
  loadError.value = ''
  if (!context?.server_backed) {
    comments.value = []
    return
  }
  try {
    const response = await api.get('/dev/trace-review/comments', {
      params: { case_id: context.case_id, run_index: context.run_index, raw: true, dataset_id: context.dataset_id, include_history: true },
      _silent: true,
    })
    if (generation === requestGeneration && requester === authIdentity()) comments.value = response.data.comments
  } catch (error) {
    if (generation !== requestGeneration || requester !== authIdentity()) return
    comments.value = []
    loadError.value = error.response?.status === 401 ? '登录已失效，请重新登录。' : error.response?.status === 403 ? '仅超级管理员可查看评论。' : '评论未加载，请稍后重试。'
  }
}
function sameTarget(a, b) {
  return !!a && !!b && ['case_id', 'run_id', 'run_index', 'scope'].every(key => (a[key] ?? null) === (b[key] ?? null)) && (a.dataset_id ?? null) === (b.dataset_id ?? null)
}
function resetComposer(context) {
  draftContext.value = context ? { ...context, anchor: 'run', quote: '', field_label: '' } : null
  reviewSubject.value = 'general'
  saved.value = false
  saveError.value = ''
  saveErrorCode.value = ''
}
function receive(event) {
  if (event.origin !== location.origin || event.source !== viewer.value?.contentWindow) return
  const message = event.data
  if (!message || typeof message !== 'object') return
  if (message.type === 'lockbot-trace-select-dataset' && message.route && typeof message.route === 'object') {
    if (message.route.dataset_id != null && (typeof message.route.dataset_id !== 'string' || !/^[a-f0-9]{64}$/.test(message.route.dataset_id))) return
    router.push({ path: '/agent-traces', query: routeFields(message.route) })
  } else if (message.type === 'lockbot-trace-route' && validContext(message.context)) {
    const expectedDataset = new URLSearchParams(new URL(viewerUrl.value, location.origin).hash.slice(1)).get('dataset_id')
    const expectedLegacy = new URLSearchParams(new URL(viewerUrl.value, location.origin).hash.slice(1)).get('legacy')
    const expectedRun = new URLSearchParams(new URL(viewerUrl.value, location.origin).hash.slice(1)).get('review_run')
    const clearsExpectedRun = expectedRun && message.navigation === 'push' && message.route?.review_run == null
    if ((message.route?.dataset_id ?? null) !== expectedDataset || (message.route?.legacy ?? null) !== expectedLegacy || ((message.route?.review_run ?? null) !== expectedRun && !clearsExpectedRun)) return
    if (clearsExpectedRun) pendingLocate = null
    current.value = message.context
    const next = routeFields(message.route || {})
    const serialized = JSON.stringify(next)
    if (serialized !== JSON.stringify(routeFields(route.query)) && requestedRoute !== serialized) {
      requestedRoute = serialized
      router[message.navigation === 'push' ? 'push' : 'replace']({ path: '/agent-traces', query: next }).finally(() => { if (requestedRoute === serialized) requestedRoute = '' })
    }
    if (reviewOpen.value && !draft.value.trim() && !sameTarget(draftContext.value, current.value)) {
      resetComposer(current.value)
      loadComments(current.value)
    }
    if (pendingLocate && sameTarget(pendingLocate, message.context)) {
      send({ type: 'lockbot-trace-locate', ...pendingLocate })
      // Lazy hydration may notify twice; keep the exact locator until navigation changes.
    }
  } else if (message.type === 'lockbot-trace-comment' && validContext(message.context)) {
    if (typeof message.anchor !== 'string' || message.anchor.length > 160 ||
        typeof message.quote !== 'string' || message.quote.length > 4000) return
    if (!reselecting.value && draft.value.trim() && (draftContext.value?.dataset_id !== message.context.dataset_id || draftContext.value?.case_id !== message.context.case_id || draftContext.value?.scope !== message.context.scope || draftContext.value?.run_id !== message.context.run_id || draftContext.value?.anchor !== message.anchor || draftContext.value?.quote !== message.quote)) {
      reviewOpen.value = true
      saveError.value = '上次的评论草稿还在，请先保存，或导出后清空草稿。'
      return
    }
    if (!reselecting.value && draft.value.trim()) { reviewOpen.value = true; return }
    reselecting.value = false
    draftContext.value = { ...message.context, anchor: message.anchor, quote: message.quote,
      field_label: typeof message.field_label === 'string' ? message.field_label.slice(0, 160) : '' }
    if (message.context.scope !== 'case' || current.value?.case_id !== message.context.case_id) current.value = message.context
    reviewSubject.value = ['general', 'scenario', 'prompt', 'expected', 'grading', 'interface'].includes(message.subject) ? message.subject : 'general'
    saved.value = false
    saveError.value = ''
    saveErrorCode.value = ''
    reviewOpen.value = true
    loadComments(message.context)
  }
}
function isUiComment(comment) {
  return typeof comment.anchor === 'string' && comment.anchor.startsWith('ui:')
}
function inCurrentDataset(comment) {
  return (comment.dataset_id ?? null) === (displayContext.value?.dataset_id ?? null)
}
const uiComments = computed(() => comments.value.filter(comment => isUiComment(comment) &&
  comment.case_id === displayContext.value?.case_id && (showResolved.value || comment.status !== 'resolved')))
const caseComments = computed(() => comments.value.filter(comment => !isUiComment(comment) && comment.scope === 'case' &&
  comment.case_id === displayContext.value?.case_id && (showResolved.value || comment.status !== 'resolved')))
const currentComments = computed(() => comments.value.filter(comment =>
  !isUiComment(comment) && inCurrentDataset(comment) && comment.scope !== 'case' && comment.raw && comment.run_id === displayContext.value?.run_id &&
  (showResolved.value || comment.status !== 'resolved')))
const historicalComments = computed(() => comments.value.filter(comment =>
  !isUiComment(comment) && comment.scope !== 'case' && comment.raw && comment.case_id === displayContext.value?.case_id && (!inCurrentDataset(comment) || comment.run_id !== displayContext.value?.run_id)))
async function save() {
  const context = draftContext.value
  if (!context?.server_backed || !draft.value.trim()) return
  const requester = authIdentity()
  saving.value = true
  saved.value = false
  saveError.value = ''
  try {
    await api.post('/dev/trace-review/comments', {
      scope: context.scope || 'run', subject: reviewSubject.value,
      case_id: context.case_id, run_index: context.run_index, run_id: context.run_id,
      dataset_id: context.dataset_id, raw: true, anchor: context.anchor, quote: context.quote, body: draft.value,
    }, { _silent: true })
    if (requester !== authIdentity()) return
    draft.value = ''
    saved.value = true
    await loadComments(context)
  } catch (error) {
    if (requester !== authIdentity()) return
    saveErrorCode.value = error.response?.data?.code || ''
    saveError.value = saveErrorCode.value === 'comment_store_full'
      ? '评论存储已满，暂时无法保存。草稿已保留，可先导出。'
      : saveErrorCode.value === 'trace_changed'
        ? '原记录已变化，草稿和引用已保留。请重新选择原文后再保存。'
        : error.response?.status === 401 ? '登录已失效，草稿已保留，请重新登录。'
          : error.response?.status === 403 ? '没有保存评论的权限，草稿已保留。'
            : '保存失败，草稿已保留，可以重试。'
  } finally {
    saving.value = false
  }
}
async function resolve(comment) {
  const requester = authIdentity()
  try {
    await api.patch('/dev/trace-review/comments/' + comment.id,
      { status: comment.status === 'resolved' ? 'open' : 'resolved' },
      { params: { dataset_id: comment.dataset_id }, _silent: true })
    if (requester === authIdentity()) await loadComments(displayContext.value)
  } catch {
    if (requester !== authIdentity()) return
    loadError.value = '未能更新评论状态，请重试。'
  }
}
function locate(comment) {
  reviewOpen.value = false
  if (isUiComment(comment)) return send({ type: 'lockbot-trace-locate', anchor: comment.anchor })
  if (sameTarget({ ...comment, scope: 'run' }, current.value)) {
    send({ type: 'lockbot-trace-locate', run_id: comment.run_id, dataset_id: comment.dataset_id, anchor: comment.anchor })
    return
  }
  if (!comment.source_available) return
  pendingLocate = { scope: 'run', case_id: comment.case_id, run_index: comment.run_index, run_id: comment.run_id, dataset_id: comment.dataset_id, anchor: comment.anchor }
  router.push({ path: '/agent-traces', query: routeFields({ case: comment.case_id, run: String(comment.run_index), dataset_id: comment.dataset_id, legacy: comment.dataset_id == null ? '1' : undefined, review_run: comment.run_id }) })
}
function clearDraft() {
  reselecting.value = false
  draft.value = ''
  resetComposer(current.value)
  if (reviewOpen.value) loadComments(current.value)
}
function cancelReselect() { reselecting.value = false; reviewOpen.value = true }
function reselectOriginal() {
  // Choosing a new comment target is explicit; never silently replace an obsolete run ID.
  reselecting.value = true
  reviewOpen.value = false
  saveError.value = '正文仍保留。请在当前记录重新选原文，再点击评论，即可继续使用这份正文。'
}

watch(draft, value => { if (value) saved.value = false })
function returnCurrent() {
  pendingLocate = null
  router.push({ path: '/agent-traces', query: routeFields({ case: route.query.case }) })
}
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
watch(() => route.query, () => {
  const fields = routeFields(route.query)
  if (pendingLocate && (fields.case !== pendingLocate.case_id || fields.run !== String(pendingLocate.run_index) || (fields.dataset_id ?? null) !== (pendingLocate.dataset_id ?? null) || (fields.legacy === '1') !== (pendingLocate.dataset_id == null))) pendingLocate = null
  const loaded = new URL(viewerUrl.value, location.origin).hash.slice(1)
  if ((new URLSearchParams(loaded).get('dataset_id') || null) !== (fields.dataset_id || null) || (new URLSearchParams(loaded).get('legacy') || null) !== (fields.legacy || null) || (new URLSearchParams(loaded).get('review_run') || null) !== (fields.review_run || null)) {
    ++viewerRevision.value
    viewerUrl.value = '/dev/trace-review?raw=true&embedded=true#' + new URLSearchParams(fields)
  } else synchronize()
})
watch(reviewOpen, value => send({ type: 'lockbot-trace-review-open', open: value }))
function authIdentity() {
  return JSON.stringify([auth.token, auth.user?.id, auth.user?.username, auth.user?.role, localStorage.getItem(LS_KEYS.token), localStorage.getItem(LS_KEYS.user)])
}
function persistDraft() {
  if (restoringDraft || !draftKey()) return
  try {
    if (draft.value) sessionStorage.setItem(draftKey(), JSON.stringify({ context: draftContext.value, subject: reviewSubject.value, body: draft.value }))
    else sessionStorage.removeItem(draftKey())
  } catch { /* Input remains usable when browser storage is unavailable. */ }
}
function restoreDraft() {
  try {
    const stored = JSON.parse(sessionStorage.getItem(draftKey()) || 'null')
    if (stored && validContext(stored.context) && typeof stored.body === 'string' && stored.body.trim()) {
      draftContext.value = stored.context
      if (Object.hasOwn(subjectLabels, stored.subject)) reviewSubject.value = stored.subject
      draft.value = stored.body.slice(0, 4000)
      reviewOpen.value = true
      loadComments(stored.context)
    }
  } catch { /* Invalid drafts do not affect loading the current trace. */ }
}
function verifiedDraftOwner() {
  try {
    const token = localStorage.getItem(LS_KEYS.token)
    const user = JSON.parse(localStorage.getItem(LS_KEYS.user) || 'null')
    if (!token || token !== auth.token || !user || !auth.user ||
        user.id !== auth.user.id || user.username !== auth.user.username || user.role !== auth.user.role) return ''
    // switchAccount changes the token before /auth/me replaces user. Do not
    // pair that new credential with the former account's drafts or comments.
    if (auth.token !== verifiedToken && auth.user === verifiedUser) return ''
    verifiedToken = auth.token
    verifiedUser = auth.user
    return String(user.id ?? user.username ?? '')
  } catch { return '' }
}
function identityChanged() {
  const next = authIdentity()
  const owner = verifiedDraftOwner()
  if (identity === next && draftOwner === owner) return
  persistDraft()
  identity = next
  ++requestGeneration
  restoringDraft = true
  draft.value = ''
  draftContext.value = null
  comments.value = []
  current.value = null
  reviewOpen.value = false
  pendingLocate = null
  reselecting.value = false
  saved.value = false
  saveErrorCode.value = ''
  saveError.value = loadError.value = ''
  draftOwner = owner
  if (draftOwner) restoreDraft()
  restoringDraft = false
}
watch([draft, draftContext, reviewSubject], persistDraft, { deep: true, flush: 'sync' })
watch(() => [auth.token, auth.user, auth.user?.id, auth.user?.username, auth.user?.role], identityChanged)
function authStorageChanged(event) {
  if (event.key === null || event.key === LS_KEYS.token || event.key === LS_KEYS.user) identityChanged()
}
onMounted(() => {
  window.addEventListener('message', receive)
  window.addEventListener('storage', authStorageChanged)
  identityChanged()
  themeObserver = new MutationObserver(synchronize)
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
  try {
    const pending = JSON.parse(sessionStorage.getItem('lockbot_trace_pending_comment') || 'null')
    if (pending?.context && validContext(pending.context)) {
      receive({ origin: location.origin, source: viewer.value.contentWindow, data: pending })
      sessionStorage.removeItem('lockbot_trace_pending_comment')
    }
  } catch {
    // Invalid drafts do not affect loading the current trace.
  }
})
onBeforeUnmount(() => {
  persistDraft()
  ++requestGeneration
  window.removeEventListener('message', receive)
  window.removeEventListener('storage', authStorageChanged)
  themeObserver?.disconnect()
})
</script>

<style scoped>
.agent-traces { width: 100%; height: 100dvh; display: flex; flex-direction: column; }
iframe { width: 100%; flex: 1; min-height: 0; border: 0; display: block; }
.review-history-banner { display: flex; align-items: center; justify-content: space-between; padding: 6px 16px; color: var(--lb-text-regular); font-size: 13px; }
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
