<template>
  <section v-if="authenticated || supported || loading || error" class="panel native-connections">
    <template v-if="supported || loading || error">
      <div class="connection-heading">
        <h2>本人连接与 Agent 接入</h2>
        <button class="connection-button" :disabled="loading" @click="refresh">
          刷新 Key 状态
        </button>
      </div>
      <p class="scope">刷新只重新检查授权和恢复状态，不更换或延长 Key。接入说明不包含 Key。</p>
      <p v-if="loading" role="status" class="scope">正在验证本人连接…</p>
      <p v-if="error" role="alert" class="connection-warning">{{ error }}</p>
      <p v-if="unavailable" class="connection-warning">
        部分绑定已失效或不可用，请让已绑定的 Agent 检查。
      </p>
      <div v-for="item in items" :key="item.id" class="connection-entry">
        <div class="connection-heading">
          <h3>{{ item.bot.name }}</h3>
          <span class="connection-state">绑定已确认</span>
        </div>
        <p class="scope">
          {{ platformName(item.actor.platform) }} · {{ item.actor.external_user_id }}
        </p>
        <p class="scope">服务 {{ item.service_origin }}</p>
        <p class="scope">
          {{ healthText(item.credential_recovery_health) }} · {{ expiryText(item.expires_at) }}
        </p>
        <p
          v-if="['legacy_missing', 'unreadable'].includes(item.credential_recovery_health)"
          class="connection-warning"
        >
          恢复副本不可用不代表本地 Key 作废。保留有效原 Key，由原 Agent 按说明检查或回填。
        </p>
        <button
          class="connection-button primary"
          :disabled="loading"
          @click="copyInstructions(item)"
        >
          复制接入说明
        </button>
        <button
          v-if="viewSupported"
          class="connection-button"
          :disabled="loading"
          @click="copyViewRequest(item)"
        >
          查看 Key：复制单次授权请求给 Agent
        </button>
        <button
          v-if="rotationSupported"
          class="connection-button"
          :disabled="loading"
          @click="copyRotationRequest(item)"
        >
          重置 Key：复制专用授权请求给 Agent
        </button>
        <details>
          <summary>查看接入说明与连接标识</summary>
          <textarea
            readonly
            :value="item.instructions"
            :aria-label="`${item.bot.name} 的无 Key 接入说明`"
          />
          <p class="scope">实例 {{ item.instance_id }} · bot {{ item.bot.uuid }}</p>
        </details>
      </div>
      <div v-if="supported" class="connection-recovery">
        <div class="connection-heading">
          <h3>连接失效或需要交给另一位 Agent</h3>
          <button class="connection-button" @click="copyRecovery">
            发送给 Agent：复制恢复指引
          </button>
        </div>
        <p class="scope">
          先检查现有私有连接；Key 过期或撤销后不能继续授权，不自动重置或重新绑定。
        </p>
        <details>
          <summary>查看恢复指引</summary>
          <textarea readonly :value="recoveryGuide" aria-label="无 Key 恢复指引" />
        </details>
        <a :href="skill.bundle_url" target="_blank" rel="noopener noreferrer">完整 Skill ZIP</a>
        <span aria-hidden="true"> · </span>
        <a :href="skill.markdown_url" target="_blank" rel="noopener noreferrer">SKILL.md</a>
      </div>
      <p v-if="feedback" role="status" class="scope">{{ feedback }}</p>
    </template>
    <template v-else>
      <h2>连接凭证管理暂不可用</h2>
      <p class="scope">此来源仅支持资源查询，凭证请由原 Agent 管理。</p>
    </template>
  </section>
</template>

<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps({ authenticated: Boolean, scopeKey: { type: String, default: '' } })
const items = ref([])
const skill = ref(null)
const recoveryGuide = ref('')
const supported = ref(false)
const viewSupported = ref(false)
const rotationSupported = ref(false)
const loading = ref(false)
const unavailable = ref(false)
const error = ref('')
const feedback = ref('')
let epoch = 0,
  controller,
  timeout,
  disposed = false
const safeText = (value, limit = 12000) =>
  typeof value === 'string' &&
  value.length <= limit &&
  !/\b(?:lbr_|lbk_|lba1\.)[A-Za-z0-9_.-]+/.test(value)
const text = (value, limit = 2048) => {
  if (!safeText(value, limit) || !value.trim()) throw new Error('invalid_metadata')
  return value
}
function installation(value) {
  if (value?.skill_id !== 'lockbot-resource-agent') throw new Error('invalid_metadata')
  const result = { skill_id: value.skill_id }
  for (const [field, path] of Object.entries({
    bundle_url: '/api/agent-skill/lockbot-resource-agent.zip',
    markdown_url: '/api/agent-skill/SKILL.md',
    manifest_url: '/api/agent-skill',
  })) {
    const url = new URL(text(value[field]))
    if (
      url.origin !== location.origin ||
      url.pathname !== path ||
      url.search ||
      url.hash ||
      url.username ||
      url.password
    )
      throw new Error('invalid_metadata')
    result[field] = url.href
  }
  return result
}
function connection(row) {
  if (
    !/^[a-f0-9]{32}$/.test(row?.id) ||
    row.service_origin !== location.origin ||
    !Number.isInteger(row.bot?.id) ||
    row.bot.id <= 0
  )
    throw new Error('invalid_metadata')
  const actor = {
    platform: text(row.actor?.platform),
    external_user_id: text(row.actor?.external_user_id),
  }
  if (actor.platform !== row.identity?.platform || actor.external_user_id !== row.identity?.subject)
    throw new Error('invalid_metadata')
  if (
    row.expires_at !== null &&
    (typeof row.expires_at !== 'string' || !Number.isFinite(Date.parse(row.expires_at)))
  )
    throw new Error('invalid_metadata')
  const instructions = text(row.instructions, 12000)
  if (
    !instructions.includes(skill.value.bundle_url) ||
    !instructions.includes(skill.value.markdown_url)
  )
    throw new Error('invalid_metadata')
  // Explicit projection: unexpected credential, digest or provider fields are
  // never retained in the page or copied into another Agent's instruction.
  return {
    id: row.id,
    service_origin: row.service_origin,
    instance_id: text(row.instance_id),
    bot: {
      id: row.bot.id,
      uuid: text(row.bot.uuid),
      name: text(row.bot.name, 128),
      type: text(row.bot.type, 32),
    },
    actor,
    expires_at: row.expires_at,
    credential_recovery_health: ['recoverable', 'legacy_missing', 'unreadable'].includes(
      row.credential_recovery_health
    )
      ? row.credential_recovery_health
      : 'unknown',
    instructions,
  }
}
function platformName(value) {
  return { infoflow: '如流 / Infoflow', feishu: '飞书 / Feishu' }[value] || value
}
async function read(path, signal) {
  const response = await fetch(`/api/personal-panel/${path}`, {
    credentials: 'same-origin',
    cache: 'no-store',
    redirect: 'error',
    referrerPolicy: 'no-referrer',
    signal,
  })
  if (!response.ok) throw Object.assign(new Error('request_failed'), { status: response.status })
  return response.json()
}
async function refresh() {
  const run = ++epoch
  controller?.abort()
  clearTimeout(timeout)
  const currentController = new AbortController()
  controller = currentController
  const signal = currentController.signal
  timeout = setTimeout(() => currentController.abort(), 15000)
  const current = () => run === epoch && !disposed && !signal.aborted
  items.value = []
  unavailable.value = false
  error.value = feedback.value = ''
  loading.value = true
  try {
    const capabilities = await read('capabilities', signal)
    if (!current()) return
    const management = capabilities.connection_management
    supported.value =
      management?.version === 1 && management.refresh_semantics === 'revalidate_only'
    const view = management?.credential_view
    viewSupported.value =
      supported.value &&
      view?.version === 1 &&
      view.purpose === 'view_current_key' &&
      view.requires_original_credential === true &&
      view.requires_matching_browser_grant === true &&
      view.single_use === true &&
      view.issue_path === '/api/personal-panel/credential-view-links'
    const rotation = management?.credential_rotation
    rotationSupported.value =
      supported.value &&
      rotation?.version === 1 &&
      rotation.purpose === 'rotate_current_key' &&
      rotation.requires_original_credential === true &&
      rotation.requires_matching_browser_grant === true &&
      rotation.single_use === true &&
      rotation.requires_typed_group === true &&
      rotation.requires_second_confirmation === true &&
      rotation.issue_path === '/api/personal-panel/credential-rotation-links'
    if (!supported.value) {
      skill.value = null
      recoveryGuide.value = ''
      return
    }
    skill.value = installation(management.skill)
    recoveryGuide.value = text(management.recovery_instructions, 12000)
    if (!props.authenticated) return
    const data = await read('connections', signal)
    if (!current()) return
    if (
      data.schema_version !== 1 ||
      !Array.isArray(data.connections) ||
      data.connections.length > 32
    )
      throw new Error('invalid_metadata')
    items.value = data.connections.map(connection)
    unavailable.value = data.some_authorizations_unavailable === true
  } catch (failure) {
    if (run !== epoch || disposed) return
    items.value = []
    error.value = [401, 403].includes(failure.status)
      ? '当前绑定已失效或不可用，请复制恢复指引发送给已绑定的 Agent。'
      : '无法确认当前连接，请稍后刷新状态；保留仍有效的本地 Key。'
  } finally {
    if (run === epoch) {
      clearTimeout(timeout)
      loading.value = false
    }
  }
}
async function copy(value) {
  const run = epoch
  if (!supported.value || !safeText(value)) return
  try {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value)
      } catch {
        if (run !== epoch || disposed) return
        fallbackCopy(value)
      }
    } else fallbackCopy(value)
    if (run === epoch && !disposed) feedback.value = '已复制无 Key 说明，可发送给 Agent。'
  } catch {
    if (run === epoch && !disposed) feedback.value = '复制失败，请展开说明并手动复制。'
  }
}
async function copyViewRequest(item) {
  if (!viewSupported.value || !props.authenticated || loading.value || !items.value.includes(item))
    return
  const target = JSON.stringify({
    service: item.service_origin,
    instance_id: item.instance_id,
    bot: item.bot,
    actor: item.actor,
  })
  await copy(
    `请按 lockbot-resource-agent Skill 核对本人身份与私密投递渠道。以下仅是连接标识，不是授权：${target}。` +
      '请使用这一连接已有的有效原 Key，在可信服务调用 POST /api/personal-panel/credential-view-links 签发 view_current_key 单次查看授权；' +
      '只私下交给本人返回的短期 view_url，由本人在已有同身份浏览器会话预览后明确确认。' +
      '不要回显 Key，不把链接放群聊，不把只读登录或资源链接升级为查看票；不重置、轮换、重新绑定或延长授权。' +
      '若原 Key 已过期、撤销或身份迁移，拒绝签发并给固定恢复指引；副本缺失时保留有效本地 Key。'
  )
}
async function copyRotationRequest(item) {
  if (
    !rotationSupported.value ||
    !props.authenticated ||
    loading.value ||
    !items.value.includes(item)
  )
    return
  const target = JSON.stringify({
    service: item.service_origin,
    instance_id: item.instance_id,
    bot: item.bot,
    actor: item.actor,
  })
  await copy(
    `请按既有 lockbot-resource-agent Skill 核对本人身份和私密投递渠道。以下仅为目标标识，不是授权：${target}。` +
      '本人准备评审这一个绑定的 Key 重置：它将使使用此身份在此群旧 Key 的所有 Agent 失效，需逐个更新。' +
      '请仅用这一个连接当前有效原 lbr，通过可信服务 POST /api/personal-panel/credential-rotation-links 签发独立 rotate_current_key 短期链接，私下交本人。' +
      '不得用只读会话、登录票、查看票或其它群 Key 升级；过期、撤销或迁移的原 Key 必须拒绝并给恢复指引。' +
      '先由本人在浏览器预览具体群和身份、输入完整群名并二次确认，不能替本人自动确认或重放提交；不延长原到期时间，不改其它连接，不在说明中回显 Key。'
  )
}
function fallbackCopy(value) {
  const element = document.createElement('textarea')
  element.value = value
  element.style.cssText = 'position:fixed;left:-9999px'
  document.body.appendChild(element)
  try {
    element.select()
    if (!document.execCommand('copy')) throw new Error('copy_failed')
  } finally {
    document.body.removeChild(element)
  }
}
function copyInstructions(item) {
  if (!props.authenticated || loading.value || !items.value.includes(item)) return
  return copy(item.instructions)
}
function copyRecovery() {
  return copy(recoveryGuide.value)
}
function healthText(value) {
  return (
    {
      recoverable: '恢复副本可用',
      legacy_missing: '旧 Key 尚无恢复副本',
      unreadable: '恢复副本暂不可读',
    }[value] || '恢复状态待确认'
  )
}
function expiryText(value) {
  return value === null ? '未配置到期时间' : `到期 ${new Date(value).toLocaleString('zh-CN')}`
}
watch(() => [props.authenticated, props.scopeKey], refresh, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => {
  disposed = true
  epoch++
  controller?.abort()
  clearTimeout(timeout)
  items.value = []
})
</script>

<style scoped>
.native-connections {
  display: grid;
  gap: 14px;
}
.connection-heading {
  display: flex;
  gap: 12px;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
}
.connection-entry {
  border-top: 1px solid var(--line);
  padding-top: 16px;
  display: grid;
  gap: 9px;
}
.connection-state {
  color: var(--muted);
  font-size: 12px;
}
.connection-warning {
  color: var(--lb-color-warning, #a46914);
}
.connection-button {
  width: fit-content;
  border: 1px solid var(--line);
  background: transparent;
  color: inherit;
  padding: 7px 12px;
  border-radius: 6px;
}
.connection-button.primary {
  color: var(--accent);
  border-color: currentColor;
}
.connection-button:disabled {
  opacity: 0.5;
  cursor: wait;
}
.connection-recovery {
  border-top: 1px solid var(--line);
  padding-top: 16px;
  display: grid;
  gap: 10px;
}
textarea {
  display: block;
  width: 100%;
  min-height: 145px;
  margin-block: 10px;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: transparent;
  color: inherit;
  line-height: 1.6;
}
</style>
