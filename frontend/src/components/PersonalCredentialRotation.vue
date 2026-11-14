<template>
  <section v-if="visible" class="panel credential-rotation">
    <h2>重置此资源群的本人 Key</h2>
    <p v-if="busy" role="status">正在验证本次专用授权…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="preview && !credential">
      <p>
        {{ preview.bot.name }} · {{ platformName(preview.actor.platform) }} ·
        {{ preview.actor.external_user_id }}
      </p>
      <p>
        服务 {{ preview.service_origin }} · 实例 {{ preview.instance_id }} · bot
        {{ preview.bot.uuid }}
      </p>
      <p class="warning">
        重置后，使用此身份在此资源群旧 Key 的所有 Agent 都立即失效，需要分别私下重新配置。其他人的
        Key 和其它群授权不受影响。
      </p>
      <p>新 Key 保留原到期时间，不延长权限；当前绑定的旧面板授权将退出，需用新 Key 单独登录。</p>
      <label
        >请输入完整资源群名：{{ preview.bot.name
        }}<input v-model="typedGroup" autocomplete="off" :disabled="busy || used"
      /></label>
      <label
        ><input v-model="acknowledged" type="checkbox" :disabled="busy || used" />我再次确认：此 Key
        的所有 Agent 都将失效，我将分别更新配置。</label
      >
      <button class="danger" :disabled="!ready" @click="confirm">
        最终确认重置并使旧 Key 失效
      </button>
    </template>
    <template v-if="credential">
      <p>
        重置已完成。新 Key 仅本次显示，请私下更新需要继续使用的 Agent，并用新 Key 重新授权登录。
      </p>
      <textarea
        readonly
        autocomplete="off"
        :value="credential"
        aria-label="本次重置后的本人新 Key"
      />
      <button @click="copyKey">复制新 Key</button>
      <p v-if="feedback" role="status">{{ feedback }}</p>
    </template>
    <button @click="close">{{ credential ? '关闭并清除新 Key' : '取消并关闭' }}</button>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { pendingCredentialRotationTicket } from '../utils/personalPanelTicket'

const props = defineProps({ authenticated: Boolean, scopeKey: { type: String, default: '' } })
const visible = ref(false),
  busy = ref(false),
  used = ref(false),
  preview = ref(null)
const typedGroup = ref(''),
  acknowledged = ref(false),
  credential = ref(''),
  error = ref(''),
  feedback = ref('')
let intent = '',
  epoch = 0,
  controller,
  expiryTimer,
  requestTimer,
  deadline = 0,
  disposed = false
const ready = computed(
  () =>
    props.authenticated &&
    preview.value &&
    !busy.value &&
    !used.value &&
    typedGroup.value === preview.value.bot.name &&
    acknowledged.value
)
const safe = (value) =>
  typeof value === 'string' &&
  value.length > 0 &&
  value.length <= 2048 &&
  !/\b(?:lbr_|lbk_|lba1\.)/.test(value)
function platformName(value) {
  return { infoflow: '如流 / Infoflow', feishu: '飞书 / Feishu' }[value] || value
}
function clear() {
  ++epoch
  controller?.abort()
  clearTimeout(expiryTimer)
  clearTimeout(requestTimer)
  intent = ''
  preview.value = null
  credential.value = typedGroup.value = error.value = feedback.value = ''
  busy.value = used.value = acknowledged.value = false
  deadline = 0
}
function close() {
  clear()
  visible.value = false
}
const alive = (run) => run === epoch && !disposed && props.authenticated
function expire() {
  clear()
  visible.value = true
  error.value = '本次授权已过期。不要重复重置；请由原 Agent 核查当前绑定，按需取得新的专用授权。'
}
function setExpiry(data, purpose) {
  if (
    data?.purpose !== purpose ||
    !Number.isFinite(data.expires_in) ||
    data.expires_in <= 0 ||
    data.expires_in > 120
  )
    throw new Error('invalid_authority')
  const next = Date.now() + data.expires_in * 1000
  deadline = deadline ? Math.min(deadline, next) : next
  clearTimeout(expiryTimer)
  expiryTimer = setTimeout(expire, Math.max(0, deadline - Date.now()))
}
async function request(path, body, run) {
  controller?.abort()
  const active = new AbortController()
  controller = active
  const until = Date.now() + 15000
  const timer = setTimeout(() => active.abort(), 15000)
  requestTimer = timer
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
    if (!response.ok) throw new Error('unavailable')
    const data = await response.json()
    if (
      !alive(run) ||
      active.signal.aborted ||
      Date.now() >= until ||
      (deadline && Date.now() >= deadline)
    )
      throw new Error('stale_response')
    return data
  } finally {
    clearTimeout(timer)
  }
}
async function begin(ticket) {
  clear()
  visible.value = true
  if (!props.authenticated) {
    error.value =
      '需要本人当前已确认的同一绑定会话及原 Key 专用重置授权；请先登录后再由 Agent 重新签发。'
    return
  }
  const run = epoch
  busy.value = true
  try {
    const data = await request('credential-rotation-preview', { ticket }, run)
    setExpiry(data, 'rotate_current_key')
    const value = data.connection
    if (
      data.service_origin !== location.origin ||
      !safe(data.intent) ||
      ![
        value?.bot?.name,
        value?.bot?.uuid,
        value?.actor?.platform,
        value?.actor?.external_user_id,
        value?.instance_id,
      ].every(safe)
    )
      throw new Error('invalid_metadata')
    intent = data.intent
    preview.value = {
      bot: { name: value.bot.name, uuid: value.bot.uuid },
      actor: { platform: value.actor.platform, external_user_id: value.actor.external_user_id },
      instance_id: value.instance_id,
      service_origin: data.service_origin,
    }
  } catch {
    if (alive(run)) {
      clear()
      visible.value = true
      error.value = '无法确认重置专用授权。原 Key 不会因为预览失败而自动更换，请由原 Agent 检查。'
    }
  } finally {
    if (alive(run)) busy.value = false
  }
}
async function confirm() {
  if (!ready.value || !intent) return
  if (Date.now() >= deadline) {
    expire()
    return
  }
  const run = epoch,
    proof = intent
  intent = ''
  busy.value = used.value = true
  try {
    const data = await request(
      'credential-rotation-confirm',
      {
        intent: proof,
        typed_group: typedGroup.value,
        confirmation: 'invalidate_all_agents_using_this_key',
      },
      run
    )
    setExpiry(data, 'rotated_current_key')
    if (
      data.requires_fresh_login !== true ||
      typeof data.credential !== 'string' ||
      !/^lbr_[A-Za-z0-9_-]{16,512}$/.test(data.credential)
    )
      throw new Error('invalid_credential')
    credential.value = data.credential
    typedGroup.value = ''
    acknowledged.value = false
  } catch {
    if (alive(run)) {
      credential.value = ''
      error.value =
        '未能确认重置结果，服务端可能已完成。不要重复点击或自动重试；请由本人在 IM 中重新确认绑定并恢复接入。'
    }
  } finally {
    if (alive(run)) busy.value = false
  }
}
async function copyKey() {
  if (!credential.value || !props.authenticated) return
  if (Date.now() >= deadline) {
    expire()
    return
  }
  const run = epoch
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard_unavailable')
    await navigator.clipboard.writeText(credential.value)
    if (deadline && Date.now() >= deadline) {
      expire()
      return
    }
    if (alive(run)) feedback.value = '已复制新 Key。请私下逐个更新需要继续使用的 Agent。'
  } catch {
    if (alive(run)) feedback.value = '复制不可用，请在当前安全环境手动复制。'
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
  pendingCredentialRotationTicket,
  (ticket) => {
    if (ticket) {
      pendingCredentialRotationTicket.value = ''
      void begin(ticket)
    }
  },
  { immediate: true, flush: 'sync' }
)
function wake() {
  if (deadline && Date.now() >= deadline) expire()
}
onMounted(() => {
  window.addEventListener('focus', wake)
  window.addEventListener('pageshow', wake)
  document.addEventListener('visibilitychange', wake)
})
onBeforeUnmount(() => {
  window.removeEventListener('focus', wake)
  window.removeEventListener('pageshow', wake)
  document.removeEventListener('visibilitychange', wake)
  disposed = true
  clear()
})
</script>

<style scoped>
.credential-rotation {
  border: 1px solid #b94440;
}
.credential-rotation p,
.credential-rotation label {
  display: block;
  overflow-wrap: anywhere;
  margin: 12px 0;
}
.credential-rotation .warning {
  color: #b94440;
  font-weight: 600;
}
.credential-rotation textarea {
  display: block;
  width: 100%;
  min-height: 80px;
}
.credential-rotation button {
  margin: 8px 12px 8px 0;
  padding: 8px 12px;
  cursor: pointer;
}
.credential-rotation .danger {
  background: #b94440;
  color: white;
}
</style>
