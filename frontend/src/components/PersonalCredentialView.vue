<template>
  <section v-if="visible" class="panel credential-view">
    <h2>单次查看本人 Key</h2>
    <p v-if="busy" role="status">正在验证本次授权…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="preview">
      <p>
        {{ preview.bot.name }} · {{ platformName(preview.actor.platform) }} ·
        {{ preview.actor.external_user_id }}
      </p>
      <p>服务实例 {{ preview.instance_id }} · bot {{ preview.bot.uuid }}</p>
      <p>这是原 Key 的短期单次查看授权，不会更换 Key。请确认当前环境适合查看私密凭证。</p>
      <p v-if="preview.credential_recovery_health !== 'recoverable'">
        恢复副本不可用。保留有效本地 Key，让原 Agent 检查或用原值回填；此处不会重置。
      </p>
      <button v-if="!credential" :disabled="busy || used" @click="reveal">确认查看原 Key</button>
    </template>
    <template v-if="credential">
      <p>仅本次临时显示。关闭、退出、切换身份或授权到期后清除页面中的 Key。</p>
      <textarea readonly autocomplete="off" :value="credential" aria-label="本次授权的本人 Key" />
      <button @click="copyKey">复制 Key</button>
      <p v-if="feedback" role="status">{{ feedback }}</p>
    </template>
    <button @click="close">关闭并清除</button>
  </section>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { pendingCredentialViewTicket } from '../utils/personalPanelTicket'

const props = defineProps({ authenticated: Boolean, scopeKey: { type: String, default: '' } })
const visible = ref(false),
  busy = ref(false),
  used = ref(false)
const preview = ref(null),
  credential = ref(''),
  error = ref(''),
  feedback = ref('')
let ticket = '',
  epoch = 0,
  controller,
  requestTimer,
  expiryTimer,
  expiresAt = 0,
  disposed = false
const safe = (value) =>
  typeof value === 'string' &&
  value.length > 0 &&
  value.length <= 2048 &&
  !/\b(?:lbr_|lbk_|lba1\.)/.test(value)
function clear() {
  ++epoch
  controller?.abort()
  clearTimeout(requestTimer)
  clearTimeout(expiryTimer)
  ticket = ''
  credential.value = ''
  preview.value = null
  busy.value = used.value = false
  feedback.value = error.value = ''
  expiresAt = 0
}
function close() {
  clear()
  visible.value = false
}
function alive(run) {
  return run === epoch && !disposed && props.authenticated
}
function platformName(value) {
  return { infoflow: '如流 / Infoflow', feishu: '飞书 / Feishu' }[value] || value
}
function expiry(data) {
  if (
    data?.purpose !== 'view_current_key' ||
    !Number.isFinite(data.expires_in) ||
    data.expires_in <= 0 ||
    data.expires_in > 120
  )
    throw new Error('invalid_authority')
  const deadline = Date.now() + data.expires_in * 1000
  expiresAt = expiresAt ? Math.min(expiresAt, deadline) : deadline
  clearTimeout(expiryTimer)
  expiryTimer = setTimeout(expire, Math.max(0, expiresAt - Date.now()))
}
function expire() {
  clear()
  visible.value = true
  error.value = '本次查看授权已过期，请让持有有效原 Key 的 Agent 重新授权。'
}
async function request(path, body, run) {
  controller?.abort()
  const active = new AbortController()
  controller = active
  const deadline = Date.now() + 15000
  const activeTimer = setTimeout(() => active.abort(), 15000)
  requestTimer = activeTimer
  try {
    const response = await fetch(`/api/personal-panel/${path}`, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
      referrerPolicy: 'no-referrer',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: active.signal,
    })
    if (!response.ok) throw new Error('authorization_unavailable')
    const data = await response.json()
    if (
      !alive(run) ||
      active.signal.aborted ||
      Date.now() >= deadline ||
      (expiresAt && Date.now() >= expiresAt)
    )
      throw new Error('stale_response')
    return data
  } finally {
    clearTimeout(activeTimer)
  }
}
async function begin(incoming) {
  clear()
  visible.value = true
  if (!props.authenticated) {
    error.value =
      '请先使用本人绑定登录，再让原 Agent 单独签发本次 Key 查看授权；只读登录不授予查看权限。'
    return
  }
  const run = epoch
  ticket = incoming
  busy.value = true
  try {
    const data = await request('credential-view-preview', { ticket }, run)
    expiry(data)
    const value = data.connection
    if (
      ![
        value?.bot?.name,
        value?.bot?.uuid,
        value?.actor?.platform,
        value?.actor?.external_user_id,
        value?.instance_id,
      ].every(safe)
    )
      throw new Error('invalid_metadata')
    preview.value = {
      bot: { name: value.bot.name, uuid: value.bot.uuid },
      actor: { platform: value.actor.platform, external_user_id: value.actor.external_user_id },
      instance_id: value.instance_id,
      credential_recovery_health: ['recoverable', 'legacy_missing', 'unreadable'].includes(
        value.credential_recovery_health
      )
        ? value.credential_recovery_health
        : 'unknown',
    }
  } catch {
    if (alive(run)) {
      clear()
      visible.value = true
      error.value = '无法确认本次查看授权，请保留有效本地 Key，由原 Agent 重新检查当前绑定。'
    }
  } finally {
    if (alive(run)) busy.value = false
  }
}
async function reveal() {
  if (!ticket || busy.value || used.value || !props.authenticated) return
  if (Date.now() >= expiresAt) {
    expire()
    return
  }
  const run = epoch
  busy.value = used.value = true
  const proof = ticket
  ticket = ''
  try {
    const data = await request(
      'credential-view',
      { ticket: proof, confirmation: 'view_current_key' },
      run
    )
    expiry(data)
    if (typeof data.credential !== 'string' || !/^lbr_[A-Za-z0-9_-]{16,512}$/.test(data.credential))
      throw new Error('invalid_credential')
    credential.value = data.credential
  } catch {
    if (alive(run)) {
      credential.value = ''
      error.value =
        '本次查看未完成，链接不能重复使用。保留有效原 Key；副本缺失时请由原 Agent 检查或原值回填。'
    }
  } finally {
    if (alive(run)) busy.value = false
  }
}
async function copyKey() {
  if (!credential.value || !props.authenticated) return
  if (Date.now() >= expiresAt) {
    expire()
    return
  }
  const run = epoch
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard_unavailable')
    await navigator.clipboard.writeText(credential.value)
    if (expiresAt && Date.now() >= expiresAt) {
      expire()
      return
    }
    if (alive(run)) feedback.value = '已复制 Key。请只在本人信任的私有连接中使用。'
  } catch {
    if (alive(run)) feedback.value = '复制不可用，可在当前安全环境手动复制。'
  }
}
watch(
  () => [props.authenticated, props.scopeKey],
  (_value, previous) => {
    if (previous) close()
  },
  { flush: 'sync' }
)
watch(
  pendingCredentialViewTicket,
  (incoming) => {
    if (!incoming) return
    pendingCredentialViewTicket.value = ''
    void begin(incoming)
  },
  { immediate: true, flush: 'sync' }
)
function wake() {
  if (expiresAt && Date.now() >= expiresAt) expire()
}
onMounted(() => {
  globalThis.window?.addEventListener('focus', wake)
  globalThis.window?.addEventListener('pageshow', wake)
  globalThis.document?.addEventListener('visibilitychange', wake)
})
onBeforeUnmount(() => {
  globalThis.window?.removeEventListener('focus', wake)
  globalThis.window?.removeEventListener('pageshow', wake)
  globalThis.document?.removeEventListener('visibilitychange', wake)
  disposed = true
  clear()
})
</script>

<style scoped>
.credential-view {
  border: 1px solid var(--lb-border-color);
}
.credential-view p {
  overflow-wrap: anywhere;
}
.credential-view textarea {
  display: block;
  width: 100%;
  min-height: 80px;
  margin: 12px 0;
}
.credential-view button {
  margin: 8px 12px 8px 0;
  padding: 8px 12px;
  cursor: pointer;
}
</style>
