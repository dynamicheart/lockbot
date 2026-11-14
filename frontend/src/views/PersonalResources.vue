<template>
  <div class="personal-panel">
    <header class="bar">
      <div class="bar-inner">
        <router-link class="wordmark" to="/">LockBot</router-link>
        <nav v-if="session.authenticated && !ticket" class="nav" aria-label="主导航">
          <button
            v-for="item in tabs"
            :key="item.id"
            :class="{ active: tab === item.id }"
            :aria-current="tab === item.id ? 'page' : undefined"
            @click="tab = item.id"
          >
            {{ item.label }}
          </button>
        </nav>
        <el-dropdown v-if="session.authenticated" class="account-menu" trigger="click">
          <el-button text :disabled="busy" aria-label="账号菜单">
            <el-icon><User /></el-icon><span class="account-label">账号</span>
            <el-icon><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="run(logout)">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>
    <main class="main" aria-live="polite">
      <div class="page-heading">
        <h1>{{ ticket ? '确认登录身份' : title }}</h1>
        <div v-if="session.authenticated" class="actions">
          <el-button :loading="busy" @click="run(refresh)">刷新资源</el-button>
        </div>
      </div>
      <el-alert v-if="error" :title="error" type="warning" :closable="false" show-icon />
      <PersonalCredentialView
        v-if="initialized"
        :authenticated="session.authenticated"
        :scope-key="identityScope(session.identities)"
      />
      <section v-if="!initialized" class="panel session-loading" role="status">
        <el-skeleton :rows="2" animated />
        <p class="scope">正在验证登录状态…</p>
      </section>
      <section v-else-if="ticket" class="panel">
        <p v-if="session.authenticated">
          当前浏览器已登录 {{ session.identities.map(identityLabel).join('、') }}。
        </p>
        <p>本次登录账号 {{ identityLabel(ticket.identity) }}</p>
        <p class="scope">确认后才会使用本次登录链接。</p>
        <div class="actions">
          <el-button type="primary" :loading="busy" @click="run(() => login('replace'))">{{
            session.authenticated ? '切换为本次身份' : '确认登录'
          }}</el-button>
          <el-button :disabled="busy" @click="ticket = null">取消</el-button>
        </div>
        <template v-if="session.authenticated">
          <el-checkbox v-model="combine" :disabled="busy"
            >我控制上述账号，选择仅在此浏览器合看</el-checkbox
          >
          <el-button v-if="combine" :loading="busy" @click="run(() => login('append'))"
            >确认在此浏览器合看</el-button
          >
          <p class="scope">各群授权分别生效，合看不会将账号认定为同一员工。</p>
        </template>
      </section>
      <section v-else-if="!session.authenticated" class="panel">
        <p>
          <a href="/api/agent-skill/SKILL.md" target="_blank" rel="noopener noreferrer"
            >Agent 使用说明（SKILL.md）</a
          >
        </p>
        <p>
          首次使用或换设备时，在你已完成 IM 绑定的 Agent 中说“打开我的资源台”。本机 CLI 和服务器上的
          Agent 均可使用，取得链接后在此浏览器打开并确认登录。
        </p>
        <p class="scope">
          登录后，当前浏览器会保留登录状态。收藏本页，下次直接打开；会话失效时再通过已绑定的 Agent
          登录。
        </p>
        <PersonalConnectionHandoff :authenticated="false" />
      </section>
      <template v-else-if="tab === 'mine'">
        <p v-if="resourceLoading" class="resource-loading" role="status">
          {{ resourcesReady ? '正在更新，当前显示上次结果…' : '正在读取资源…' }}
        </p>
        <div class="stats">
          <div v-for="item in statistics" :key="item.label" class="stat">
            <div class="stat-label">{{ item.label }}</div>
            <el-skeleton
              v-if="initialResourceLoading"
              animated
              class="stat-placeholder"
              aria-hidden="true"
            >
              <template #template
                ><el-skeleton-item variant="text" class="stat-skeleton"
              /></template>
            </el-skeleton>
            <div v-else-if="item.value === null" class="stat-value stat-pending">待确认</div>
            <div v-else class="stat-value">
              {{ item.value }}<span>{{ item.unit }}</span>
            </div>
          </div>
        </div>
        <p v-if="resourcesReady || groups.length" class="scope">
          已确认 {{ knownGroups.length }} / {{ groups.length }} 个群 ·
          仅统计已确认占用，机器按群计数
        </p>
        <el-alert
          v-if="pendingGroups"
          :title="`${pendingGroups} 个群的数据待确认，统计可能不完整。`"
          type="warning"
          :closable="false"
          show-icon
        />
        <section v-for="section in allocationSections" :key="section.label" class="panel">
          <h2 class="panel-title">{{ section.label }} · {{ section.rows.length }}</h2>
          <div v-if="section.rows.length" class="table-scroll">
            <el-table :data="section.rows" class="allocation-table">
              <el-table-column label="资源群" min-width="160"
                ><template #default="{ row }">{{ row.group.label }}</template></el-table-column
              >
              <el-table-column label="节点 / 卡号" min-width="160"
                ><template #default="{ row }"
                  >{{ row.allocation.node ?? '未知节点'
                  }}{{
                    row.allocation.device == null ? '' : ' / 卡 ' + row.allocation.device
                  }}</template
                ></el-table-column
              >
              <el-table-column label="状态" min-width="100"
                ><template #default="{ row }"
                  ><el-tag :type="fresh(row.group) ? 'success' : 'warning'">{{
                    fresh(row.group) ? section.status : '待确认'
                  }}</el-tag></template
                ></el-table-column
              >
              <el-table-column label="本人身份" min-width="120"
                ><template #default="{ row }">{{
                  row.group.identity?.subject
                }}</template></el-table-column
              >
              <el-table-column label="到期时间" min-width="170"
                ><template #default="{ row }">{{
                  date(row.allocation.expires_at)
                }}</template></el-table-column
              >
              <el-table-column label="源数据时间" min-width="170"
                ><template #default="{ row }">{{
                  date(row.group.source_at)
                }}</template></el-table-column
              >
            </el-table>
          </div>
          <p v-else class="empty">
            {{ resourcesReady ? '已确认的群中暂无记录。' : '资源信息待更新。' }}
          </p>
        </section>
      </template>
      <div v-else-if="tab === 'groups'" class="group-grid">
        <section v-if="initialResourceLoading" class="group-card" role="status">
          <el-skeleton :rows="2" animated />
          <p class="group-meta">正在读取资源群…</p>
        </section>
        <section v-for="(group, index) in groups" :key="index" class="group-card">
          <div class="page-heading">
            <h2>{{ group.label }}</h2>
            <el-tag :type="fresh(group) ? 'success' : 'warning'">{{
              fresh(group) ? '已更新' : '待确认'
            }}</el-tag>
          </div>
          <p class="group-meta">{{ identityLabel(group.identity) }}</p>
          <div v-if="fresh(group)" class="group-metrics">
            <div v-for="item in groupCounts(group)" :key="item.label">
              <strong>{{ item.value }}</strong
              ><span>{{ item.label }}</span>
            </div>
          </div>
          <p v-else class="group-meta group-pending">暂未获取已确认的占用信息</p>
          <p class="group-meta">源数据时间 {{ date(group.source_at) }}</p>
        </section>
        <p v-if="!groups.length && !initialResourceLoading" class="empty">
          {{ resourcesReady ? '当前没有已授权的群资源。' : '资源信息待更新。' }}
        </p>
      </div>
      <template v-else>
        <PersonalConnectionHandoff
          :authenticated="session.authenticated"
          :scope-key="identityScope(session.identities)"
        />
        <details class="connection-help">
          <summary>如何添加资源群</summary>
          <p class="scope">
            在持有目标群绑定的 Agent 中说“打开我的资源台”，打开链接后确认添加到当前浏览器。
          </p>
        </details>
        <section
          v-for="(identity, index) in session.identities"
          :key="index"
          class="group-card connection-card"
        >
          <div class="connection-heading">
            <h2>登录身份 · {{ identity.subject }}</h2>
            <el-tag type="info" size="small">{{ platformName(identity.platform) }}</el-tag>
          </div>
          <div
            v-for="source in identitySources(identity)"
            :key="source.kind + source.label"
            class="connection-groups"
          >
            <span class="group-meta">{{ source.label }} · 当前可见资源群</span>
            <el-tag v-for="name in source.groups" :key="name" type="info" effect="plain">{{
              name
            }}</el-tag>
            <p v-if="source.kind !== 'identity_provider'" class="group-meta">
              此来源仅支持资源查询，凭证请由原 Agent 管理；原生 Key 管理能力不可用。
            </p>
          </div>
          <p v-if="!identitySources(identity).length" class="group-meta">
            {{ resourceLoading ? '正在读取…' : '暂无资源群信息' }}
          </p>
          <details class="connection-details">
            <summary>连接详情</summary>
            <dl>
              <dt>服务地址</dt>
              <dd>{{ identity.issuer }}</dd>
              <dt>账号</dt>
              <dd>{{ identity.subject }}</dd>
            </dl>
            <details class="connection-diagnostics">
              <summary>诊断信息</summary>
              <dl>
                <dt>身份命名空间</dt>
                <dd>{{ identity.realm }}</dd>
                <dt>授权范围</dt>
                <dd>{{ identity.scopes?.join('、') }}</dd>
              </dl>
            </details>
          </details>
        </section>
        <p
          v-for="(indices, index) in session.same_person_groups || []"
          :key="index"
          class="group-meta"
        >
          组织目录确认属于同一人
          {{
            indices.map((i) => identityLabel(session.identities[i])).join('、')
          }}。各群授权分别生效。
        </p>
      </template>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ArrowDown, User } from '@element-plus/icons-vue'
import { pendingPanelTicket } from '../utils/personalPanelTicket'
import PersonalConnectionHandoff from '../components/PersonalConnectionHandoff.vue'
import PersonalCredentialView from '../components/PersonalCredentialView.vue'

const tabs = [
  { id: 'mine', label: '我的占用' },
  { id: 'groups', label: '资源群' },
  { id: 'connections', label: '我的连接' },
]
const tab = ref('mine')
const title = computed(() => tabs.find((item) => item.id === tab.value).label)
const session = ref({ authenticated: false, identities: [] })
const groups = ref([])
const ticket = ref(null)
const combine = ref(false)
const error = ref('')
const busy = ref(false)
const initialized = ref(false)
const resourcesReady = ref(false)
const resourceLoading = ref(false)
const initialResourceLoading = computed(
  () => resourceLoading.value && !resourcesReady.value && !groups.value.length
)
const knownGroups = computed(() => groups.value.filter(fresh))
const pendingGroups = computed(() => groups.value.length - knownGroups.value.length)
const allocationSections = computed(() => {
  const rows = groups.value.flatMap((group) =>
    entries(group).map((allocation) => ({ group, allocation }))
  )
  return [
    { label: '持有资源', status: '持有中', rows: rows.filter((row) => !queued(row.allocation)) },
    { label: '预约与队列', status: '预约中', rows: rows.filter((row) => queued(row.allocation)) },
  ]
})
const statistics = computed(() => {
  const machines = knownGroups.value.reduce(
    (count, group) =>
      count +
      new Set(
        entries(group)
          .filter((a) => !queued(a) && a.node != null)
          .map((a) => a.node)
      ).size,
    0
  )
  const cards = knownGroups.value.reduce(
    (count, group) =>
      count +
      new Set(
        entries(group)
          .filter((a) => !queued(a) && a.node != null && a.device != null)
          .map((a) => JSON.stringify([a.node, a.device]))
      ).size,
    0
  )
  const bookings = knownGroups.value.reduce(
    (count, group) => count + entries(group).filter(queued).length,
    0
  )
  return [
    ['持有机器', machines, '台'],
    ['持有卡', cards, '张'],
    ['预约', bookings, '项'],
  ].map(([label, value, unit]) => ({
    label,
    value:
      resourcesReady.value && (!groups.value.length || knownGroups.value.length) ? value : null,
    unit,
  }))
})
function fresh(group) {
  return ['ok', 'fresh', 'active', 'ready', 'available', 'healthy'].includes(group.status)
}
function entries(group) {
  return Array.isArray(group.allocations) ? group.allocations : []
}
function queued(allocation) {
  return ['reservation', 'queue', 'queued'].includes(allocation.kind)
}
function platformName(value) {
  return { infoflow: '如流 / Infoflow', feishu: '飞书 / Feishu' }[value] || value || '未知平台'
}
function identityLabel(identity) {
  return `${platformName(identity?.platform)} · ${identity?.subject || ''}`
}
function identitySources(identity) {
  const sources = new Map()
  for (const group of groups.value) {
    if (
      !['issuer', 'platform', 'realm', 'subject'].every(
        (key) => group.identity?.[key] === identity[key]
      )
    )
      continue
    const kind = group.source?.kind || 'unknown'
    const label = group.source?.label || '资源来源待确认'
    const key = JSON.stringify([kind, label])
    if (!sources.has(key)) sources.set(key, { kind, label, groups: [] })
    sources.get(key).groups.push(group.label)
  }
  return [...sources.values()]
}
function groupCounts(group) {
  return [
    ['持有记录', entries(group).filter((a) => !queued(a)).length],
    ['预约记录', entries(group).filter(queued).length],
  ].map(([label, value]) => ({ label, value: fresh(group) ? value : null }))
}
function date(value) {
  if (value == null) return '未提供'
  const time = new Date(typeof value === 'number' ? value * 1000 : value)
  return Number.isNaN(time.getTime()) ? '未知' : time.toLocaleString('zh-CN')
}
async function api(path, body) {
  const response = await fetch(`/api/personal-panel/${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    redirect: 'error',
    referrerPolicy: 'no-referrer',
    ...(body === undefined
      ? {}
      : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  })
  if (!response.ok)
    throw new Error(
      response.status === 410
        ? '登录链接已过期或已使用，请回到已绑定的 Agent 重新获取。'
        : '无法更新资源或登录状态，请稍后刷新；绑定也可能已失效。'
    )
  return response.json()
}
async function run(action) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await action()
  } catch (failure) {
    error.value = failure.message
  } finally {
    busy.value = false
  }
}
let refreshEpoch = 0
function identityScope(identities) {
  return JSON.stringify(
    (identities || [])
      .map((identity) => [
        ...['issuer', 'platform', 'realm', 'subject', 'scope'].map((field) => identity[field]),
        [...(identity.scopes || [])].sort(),
      ])
      .map((values) => JSON.stringify(values))
      .sort()
  )
}
async function refresh() {
  const epoch = ++refreshEpoch
  resourceLoading.value = true
  try {
    let current
    try {
      current = await api('session')
    } catch (failure) {
      if (epoch === refreshEpoch) {
        groups.value = []
        resourcesReady.value = false
      }
      throw failure
    }
    if (epoch !== refreshEpoch) return
    if (
      !current.authenticated ||
      identityScope(current.identities) !== identityScope(session.value.identities)
    ) {
      groups.value = []
      resourcesReady.value = false
    }
    session.value = current
    // The session is ready independently of slow resource reporters.
    initialized.value = true
    if (!current.authenticated) return
    try {
      const data = await api('resources')
      if (epoch !== refreshEpoch) return
      if (!Array.isArray(data.groups)) throw new Error('资源响应无效，请稍后刷新。')
      groups.value = data.groups
      resourcesReady.value = true
    } catch (failure) {
      if (epoch === refreshEpoch) {
        groups.value = groups.value.map((group) => ({ ...group, status: 'unknown' }))
        resourcesReady.value = false
      }
      throw failure
    }
  } catch (failure) {
    if (epoch === refreshEpoch && !initialized.value) groups.value = []
    throw failure
  } finally {
    if (epoch === refreshEpoch) resourceLoading.value = false
  }
}

async function login(mode) {
  await api('login', { ticket: ticket.value.token, mode })
  ticket.value = null
  combine.value = false
  await refresh()
  if (!session.value.authenticated)
    throw new Error('登录后未检测到会话，请检查浏览器 Cookie 设置。')
}
async function logout() {
  ++refreshEpoch
  resourceLoading.value = false
  await api('logout', {})
  session.value = { authenticated: false, identities: [] }
  groups.value = []
  resourcesReady.value = false
  resourceLoading.value = false
  ticket.value = null
}
watch([pendingPanelTicket, busy, initialized], ([incoming, running, ready]) => {
  if (!incoming || running || !ready) return
  pendingPanelTicket.value = ''
  void run(async () => {
    const preview = await api('preview', { ticket: incoming })
    if (!preview.identity || typeof preview.identity !== 'object')
      throw new Error('无法确认待登录身份，请重新获取链接。')
    combine.value = false
    ticket.value = { token: incoming, identity: preview.identity }
  })
})
onMounted(async () => {
  await run(refresh)
  initialized.value = true
})
</script>

<style scoped src="../assets/styles/personal-panel.css"></style>

<style scoped>
.stat-placeholder {
  width: 88px;
  margin-top: 8px;
}
.stat-skeleton {
  width: 88px;
  height: 32px;
}
.stat-value.stat-pending {
  font-size: 18px;
  font-weight: 500;
  color: var(--lb-text-secondary);
}
.group-pending {
  min-height: 48px;
  display: flex;
  align-items: center;
}
</style>
