<template>
  <section
    v-if="supported || loading || error"
    class="native-connections"
    aria-labelledby="agent-credentials"
  >
    <div class="connection-heading credential-overview">
      <div>
        <h2 id="agent-credentials">Agent 凭证</h2>
        <p v-if="supported && authenticated && !loading && !error" class="scope">
          {{ items.length }} 个凭证 · {{ boundGroupCount }} 个绑定群
        </p>
      </div>
      <div class="credential-actions">
        <button
          v-if="items.length > 1"
          class="connection-button primary"
          :disabled="loading"
          @click="copyAllInstructions"
        >
          复制全部接入说明
        </button>
        <button v-if="authenticated" class="connection-button" :disabled="loading" @click="refresh">
          刷新状态
        </button>
        <span v-if="feedbackTarget === 'all'" class="copy-status" role="status">{{
          feedback
        }}</span>
      </div>
    </div>
    <p v-if="loading" role="status" class="scope">正在检查授权…</p>
    <p v-if="error" role="alert" class="connection-warning">{{ error }}</p>
    <p v-if="unavailable" class="connection-warning">部分绑定已失效或不可用。</p>
    <p v-if="items.length" class="scope">接入说明可发给多个 Agent，内容不含 Key。</p>
    <p v-else-if="supported && authenticated && !error && !loading" class="scope">
      当前浏览器没有可用的 Agent 凭证。可通过已绑定的 Agent 添加资源群。
    </p>
    <table v-if="items.length" class="credential-table" aria-label="Agent 凭证列表">
      <thead>
        <tr>
          <th scope="col">资源群</th>
          <th scope="col">绑定身份</th>
          <th scope="col">操作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="item in items" :key="item.id">
          <tr class="connection-entry">
            <th scope="row">{{ item.bot.name }}</th>
            <td class="credential-identity">
              <span>{{ item.actor.external_user_id }}</span>
              <small>
                {{ platformName(item.actor.platform) }}
                <template v-if="item.expires_at"> · {{ expiryText(item.expires_at) }}</template>
              </small>
            </td>
            <td class="credential-controls">
              <div class="credential-actions">
                <button
                  class="connection-button primary"
                  :disabled="loading"
                  @click="copyInstructions(item)"
                >
                  复制接入说明
                </button>
                <button
                  class="connection-button"
                  aria-haspopup="dialog"
                  :aria-label="`${item.bot.name}的详情`"
                  @click="openDetails(item)"
                >
                  详情
                </button>
              </div>
              <span v-if="feedbackTarget === item.id" class="copy-status" role="status">{{
                feedback
              }}</span>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
    <el-dialog
      v-model="detailsOpen"
      :title="selectedItem?.bot.name || '凭证详情'"
      width="min(620px, calc(100% - 24px))"
      append-to-body
      destroy-on-close
    >
      <div v-if="selectedItem" class="credential-dialog">
        <template v-for="item in [selectedItem]" :key="item.id">
          <dl class="credential-facts">
            <div>
              <dt>绑定身份</dt>
              <dd>{{ item.actor.external_user_id }} · {{ platformName(item.actor.platform) }}</dd>
            </div>
            <div>
              <dt>有效期</dt>
              <dd>{{ expiryText(item.expires_at) }}</dd>
            </div>
            <div>
              <dt>服务地址</dt>
              <dd>{{ item.service_origin }}</dd>
            </div>
            <div>
              <dt>恢复状态</dt>
              <dd>{{ healthText(item.credential_recovery_health) }}</dd>
            </div>
          </dl>
          <PersonalAgentActivity v-if="item.agentActivity" :activity="item.agentActivity" />
          <p
            v-if="['legacy_missing', 'unreadable'].includes(item.credential_recovery_health)"
            class="connection-warning"
          >
            请保留本地有效 Key，并让原 Agent 检查恢复副本。
          </p>
          <div class="credential-actions">
            <button
              v-if="viewSupported"
              class="connection-button"
              :disabled="loading"
              @click="copyViewRequest(item)"
            >
              请求查看 Key
            </button>
            <button
              v-if="rotationSupported"
              class="connection-button"
              :disabled="loading"
              @click="copyRotationRequest(item)"
            >
              请求重置 Key
            </button>
            <button
              v-if="item.recovery_instructions || skill"
              class="connection-button"
              @click="copyConnectionRecovery(item)"
            >
              复制恢复指引
            </button>
          </div>
          <span v-if="feedbackTarget === item.id + '-more'" class="copy-status" role="status">{{
            feedback
          }}</span>
          <p v-if="rotationSupported" class="scope">重置后，使用此 Key 的所有 Agent 都需要更新。</p>
          <p v-if="item.skill || skill" class="scope">
            <a :href="(item.skill || skill).bundle_url" target="_blank" rel="noopener noreferrer"
              >下载 Skill</a
            >
            ·
            <a :href="(item.skill || skill).markdown_url" target="_blank" rel="noopener noreferrer"
              >SKILL.md</a
            >
          </p>
          <textarea
            readonly
            :value="item.instructions"
            :aria-label="`${item.bot.name} 的无 Key 接入说明`"
          />
          <p class="scope">实例 {{ item.instance_id }} · bot {{ item.bot.uuid }}</p>
        </template>
      </div>
      <template #footer>
        <el-button @click="detailsOpen = false">关闭</el-button>
      </template>
    </el-dialog>
    <details v-if="supported && skill && !items.length" class="connection-recovery">
      <summary>如何找回连接</summary>
      <button class="connection-button" @click="copyRecovery">复制恢复指引</button>
      <span v-if="feedbackTarget === 'recovery'" class="copy-status" role="status">{{
        feedback
      }}</span>
      <textarea readonly :value="recoveryGuide" aria-label="无 Key 恢复指引" />
      <a :href="skill.bundle_url" target="_blank" rel="noopener noreferrer">下载 Skill</a>
    </details>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import PersonalAgentActivity from './PersonalAgentActivity.vue'
import { credentialAgentActivity } from '../utils/agentActivity'

const props = defineProps({ authenticated: Boolean, scopeKey: { type: String, default: '' } })
const items = ref([])
const boundGroupCount = computed(
  () =>
    new Set(items.value.map(({ instance_id, bot }) => JSON.stringify([instance_id, bot.uuid]))).size
)
const skill = ref(null)
const recoveryGuide = ref('')
const supported = ref(false)
const viewSupported = ref(false)
const rotationSupported = ref(false)
const loading = ref(false)
const unavailable = ref(false)
const error = ref('')
const feedback = ref('')
const feedbackTarget = ref('')
const selectedId = ref('')
const detailsOpen = ref(false)
const selectedItem = computed(() => items.value.find(({ id }) => id === selectedId.value))
function openDetails(item) {
  selectedId.value = item.id
  detailsOpen.value = true
}
let copySequence = 0
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
function installation(value, origin = location.origin) {
  if (value?.skill_id !== 'lockbot-resource-agent') throw new Error('invalid_metadata')
  const result = { skill_id: value.skill_id }
  for (const [field, path] of Object.entries({
    bundle_url: '/api/agent-skill/lockbot-resource-agent.zip',
    markdown_url: '/api/agent-skill/SKILL.md',
    manifest_url: '/api/agent-skill',
  })) {
    const url = new URL(text(value[field]))
    if (
      url.origin !== origin ||
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
function connection(row, pins = null) {
  const source = pins ? pins.get(row?.service_origin) : null
  const sourceOrigin = source ? row.service_origin : location.origin
  if (pins && (!source || source.instance_id !== row.instance_id))
    throw new Error('invalid_metadata')
  const sourceSkill = pins ? installation(row.skill, sourceOrigin) : skill.value
  if (
    !/^[a-f0-9]{32}$/.test(row?.id) ||
    row.service_origin !== sourceOrigin ||
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
    !instructions.includes(sourceSkill.bundle_url) ||
    !instructions.includes(sourceSkill.markdown_url)
  )
    throw new Error('invalid_metadata')
  if (
    pins &&
    (!text(row.recovery_instructions, 12000).includes(sourceSkill.bundle_url) ||
      !row.recovery_instructions.includes(sourceSkill.markdown_url))
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
    agentActivity: pins ? null : credentialAgentActivity(row.agent_activity),
    ...(pins
      ? { skill: sourceSkill, recovery_instructions: text(row.recovery_instructions, 12000) }
      : {}),
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
  error.value = feedback.value = feedbackTarget.value = selectedId.value = ''
  detailsOpen.value = false
  loading.value = true
  try {
    const capabilities = await read('capabilities', signal)
    if (!current()) return
    const management = capabilities.connection_management
    supported.value =
      [1, 2].includes(management?.version) &&
      management.refresh_semantics === 'revalidate_only' &&
      (management.version === 1 || management.metadata_available === true)
    const view = management?.credential_view
    viewSupported.value =
      management?.version === 1 &&
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
    skill.value = management.version === 1 ? installation(management.skill) : null
    recoveryGuide.value =
      management.version === 1 ? text(management.recovery_instructions, 12000) : ''
    if (!props.authenticated) return
    const data = await read('connections', signal)
    if (!current()) return
    if (
      data.schema_version !== management.version ||
      !Array.isArray(data.connections) ||
      data.connections.length > 32
    )
      throw new Error('invalid_metadata')
    let pins = null
    if (management.version === 2) {
      if (
        !Array.isArray(data.service_pins) ||
        !data.service_pins.length ||
        data.service_pins.length > 16
      )
        throw new Error('invalid_metadata')
      pins = new Map()
      for (const pin of data.service_pins) {
        const url = new URL(text(pin.origin))
        if (
          !['http:', 'https:'].includes(url.protocol) ||
          url.origin !== pin.origin ||
          pin.service_id !== 'lockbot.resource-api' ||
          pin.api_version !== '1' ||
          pins.has(pin.origin)
        )
          throw new Error('invalid_metadata')
        pins.set(pin.origin, { instance_id: text(pin.instance_id) })
      }
    }
    const unique = new Map()
    for (const row of data.connections) {
      const item = connection(row, pins)
      if (!unique.has(item.id)) unique.set(item.id, item)
    }
    items.value = [...unique.values()]
    unavailable.value = data.some_authorizations_unavailable === true
  } catch (failure) {
    if (run !== epoch || disposed) return
    items.value = []
    error.value = [401, 403].includes(failure.status)
      ? skill.value
        ? '当前绑定已失效或不可用，请复制恢复指引发送给已绑定的 Agent。'
        : '当前绑定已失效或不可用，请让原 Agent 检查连接。'
      : '无法确认当前连接，请稍后刷新状态；保留仍有效的本地 Key。'
  } finally {
    if (run === epoch) {
      clearTimeout(timeout)
      loading.value = false
    }
  }
}
async function copy(value, limit, target) {
  const run = epoch
  const sequence = ++copySequence
  const current = () => run === epoch && sequence === copySequence && !disposed
  if (!supported.value || !safeText(value, limit)) return
  try {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value)
      } catch {
        if (!current()) return
        fallbackCopy(value)
      }
    } else fallbackCopy(value)
    if (current()) {
      feedbackTarget.value = target
      feedback.value = '已复制无 Key 说明'
    }
  } catch {
    if (current()) {
      feedbackTarget.value = target
      feedback.value = '复制失败，请稍后重试。'
    }
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
      '若原 Key 已过期、撤销或身份迁移，拒绝签发并给固定恢复指引；副本缺失时保留有效本地 Key。',
    undefined,
    item.id + '-more'
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
      '先由本人在浏览器预览具体群和身份、输入完整群名并二次确认，不能替本人自动确认或重放提交；不延长原到期时间，不改其它连接，不在说明中回显 Key。',
    undefined,
    item.id + '-more'
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
  return copy(item.instructions, undefined, item.id)
}
function copyAllInstructions() {
  if (!props.authenticated || loading.value || items.value.length < 2) return
  const workbench = new URL('/#/me', location.origin).href
  const introduction = [
    '请安装或使用 lockbot-resource-agent Skill，为下列资源群分别保存连接。',
    '先核对服务地址、机器人和身份，逐个验证已有 Key。缺少 Key 时按对应连接的指引处理。',
    `个人资源台：${workbench}`,
  ].join('\n')
  const instructions = items.value.map((item, index) => `${index + 1}. ${item.instructions}`)
  return copy([introduction, ...instructions].join('\n\n'), 400000, 'all')
}
function copyConnectionRecovery(item) {
  if (!props.authenticated || loading.value || !items.value.includes(item)) return
  return copy(item.recovery_instructions || recoveryGuide.value, undefined, item.id + '-more')
}
function copyRecovery() {
  return copy(recoveryGuide.value, undefined, 'recovery')
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
  gap: 12px;
  min-width: 0;
}
.native-connections h2,
.native-connections h3,
.native-connections .scope {
  margin: 0;
}
.credential-overview .scope,
.credential-identity .scope {
  margin-top: 5px;
}
.connection-heading,
.credential-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.connection-heading {
  justify-content: space-between;
}
.credential-table {
  width: 100%;
  table-layout: fixed;
  border-spacing: 0;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--el-bg-color);
  overflow: hidden;
}
.credential-table thead {
  background: var(--el-fill-color-light);
  color: var(--muted);
  font-size: 13px;
}
.credential-table thead th:last-child {
  width: 240px;
}
.credential-table th,
.credential-table td {
  padding: 10px 14px;
  text-align: left;
  overflow-wrap: anywhere;
}
.credential-table tbody tr > * {
  border-top: 1px solid var(--line);
}
.credential-table tbody th {
  font-size: 15px;
}
.credential-identity small {
  display: block;
  margin-top: 3px;
}
.credential-dialog {
  --line: var(--el-border-color);
  --muted: var(--el-text-color-secondary);
  display: grid;
  gap: 12px;
  overflow-wrap: anywhere;
  max-height: 58vh;
  overflow-y: auto;
}
.credential-dialog p,
.credential-facts,
.credential-facts dd {
  margin: 0;
}
.credential-facts {
  display: grid;
  gap: 8px;
  grid-template-columns: repeat(auto-fit, minmax(min(180px, 100%), 1fr));
}
.credential-facts dt {
  color: var(--muted);
  font-size: 12px;
}
.credential-facts dd {
  font-size: 14px;
  margin-top: 2px;
}
.connection-recovery[open] > :not(:last-child) {
  margin-bottom: 9px;
}
.connection-recovery > summary {
  cursor: pointer;
  padding-block: 6px;
  color: var(--muted);
}
.credential-controls .copy-status {
  display: block;
  margin-top: 4px;
}
@media (max-width: 580px) {
  .credential-table thead {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  .credential-table tbody,
  .credential-table tr,
  .credential-table td,
  .credential-table tbody th {
    display: block;
  }
  .credential-table .connection-entry {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    border-top: 1px solid var(--line);
    padding: 9px 10px;
    gap: 7px;
    align-items: start;
  }
  .credential-table tbody tr:first-child {
    border-top: 0;
  }
  .credential-table .connection-entry > * {
    border-top: 0;
    padding: 0;
  }
  .credential-table .credential-controls {
    grid-column: 1 / -1;
  }
}
.connection-warning {
  color: var(--lb-color-warning, #a46914);
  margin: 0;
}
.connection-button {
  background: var(--el-bg-color);
  border: 1px solid var(--line);
  color: inherit;
  padding: 7px 11px;
  border-radius: 6px;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.connection-button.primary {
  color: var(--accent);
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-7);
  font-weight: 600;
}
.connection-button:disabled {
  opacity: 0.5;
  cursor: wait;
}
.copy-status {
  font-size: 13px;
  color: var(--muted);
}
textarea {
  display: block;
  box-sizing: border-box;
  width: 100%;
  min-height: 105px;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--el-fill-color-light);
  color: inherit;
  line-height: 1.6;
}
</style>
