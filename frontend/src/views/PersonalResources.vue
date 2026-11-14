<template>
  <div class="personal-panel">
    <header class="bar">
      <div class="bar-inner">
        <LockbotWordmark />
        <nav v-if="session.authenticated && !ticket" class="nav" aria-label="主导航">
          <button
            v-for="item in tabs"
            :key="item.id"
            :class="{ active: tab === item.id }"
            :aria-current="tab === item.id ? 'page' : undefined"
            @click="navigateTab(item.id)"
          >
            {{ item.label }}
          </button>
        </nav>
        <el-button
          v-if="session.authenticated"
          class="workbench-exit"
          text
          :disabled="busy"
          @click="run(logout)"
        >
          清除此浏览器授权
        </el-button>
      </div>
    </header>
    <main class="main" aria-live="polite">
      <nav
        v-if="detailPage && session.authenticated && !ticket"
        class="workbench-breadcrumb"
        aria-label="当前位置"
      >
        <router-link :to="detailPage === 'group' ? '/me/groups' : '/me'">
          {{ detailPage === 'group' ? '资源群' : '我的占用' }}
        </router-link>
        <el-icon aria-hidden="true"><ArrowRight /></el-icon>
        <span aria-current="page">{{ detailSectionTitle }}</span>
      </nav>
      <div class="page-heading">
        <h1 ref="pageTitle" tabindex="-1">{{ ticket ? '确认登录身份' : title }}</h1>
        <div v-if="session.authenticated && tab !== 'connections'" class="actions">
          <el-button :loading="busy" @click="run(refresh)">刷新资源</el-button>
        </div>
      </div>
      <el-alert v-if="error" :title="error" type="warning" :closable="false" show-icon />
      <PersonalCredentialView
        v-if="initialized"
        :authenticated="session.authenticated"
        :scope-key="identityScope(session.identities)"
      />
      <PersonalCredentialRotation
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
      <template v-else-if="detailPage">
        <section v-if="!detail" class="panel">
          <p class="scope">
            此明细已失效，请返回{{ detailPage === 'group' ? '资源群' : '我的占用' }}重新选择。
          </p>
        </section>
        <template v-else>
          <ResourceUtilization
            v-for="{ connection, configuration, nodes } in detailUtilizationGroups"
            :key="connection"
            :bot-id="connection"
            :scope-key="connection"
            :source="configuration"
            :nodes="nodes"
            :authorize="(source, options) => authorizeUtilization(connection, source, options)"
            :allowed="!ticket"
            hide-toolbar
            @change="updateUtilizationSnapshot(connection, $event)"
          />
          <div
            v-if="
              !detail?.resource &&
              (detailSections.some((section) => section.total) || allocationQuery)
            "
            class="allocation-controls"
            :class="{ 'allocation-controls-scoped': detail?.groupKey }"
          >
            <el-input
              v-model="allocationQuery"
              clearable
              aria-label="搜索资源"
              placeholder="搜索节点、卡号或用途"
            />
            <el-select
              v-if="!detail?.groupKey"
              v-model="allocationGroup"
              aria-label="筛选资源群"
              placeholder="全部资源群"
              clearable
            >
              <el-option
                v-for="group in allocationGroups"
                :key="group.value"
                :label="group.label"
                :value="group.value"
              />
            </el-select>
            <el-select v-model="allocationSort" aria-label="排序方式">
              <el-option label="即将到期优先" value="soonest" />
              <el-option label="剩余时间最长优先" value="latest" />
              <el-option label="按资源群" value="group" />
              <el-option label="按节点 / 卡号" value="resource" />
            </el-select>
          </div>
          <section v-for="section in detailSections" :key="section.label" class="panel">
            <h2 class="panel-title">{{ section.label }} · {{ section.rows.length }}</h2>
            <table v-if="section.rows.length" class="allocation-table" role="table">
              <thead role="rowgroup">
                <tr role="row">
                  <th scope="col" role="columnheader">资源群</th>
                  <th scope="col" role="columnheader">资源</th>
                  <th v-if="section.timeLabel" scope="col" role="columnheader">
                    {{ section.timeLabel }}
                  </th>
                  <th v-if="session.identities.length > 1" scope="col" role="columnheader">
                    本人身份
                  </th>
                  <th v-if="section.tasks" scope="col" role="columnheader">用途</th>
                  <th v-if="section.metrics" scope="col" role="columnheader">利用率</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                <template v-for="item in section.items" :key="item.key">
                  <tr v-if="item.rows.length > 1" role="row" class="allocation-node-summary">
                    <td role="cell" class="allocation-group">{{ item.first.group.label }}</td>
                    <td role="cell" class="allocation-name">
                      <button
                        class="allocation-disclosure"
                        type="button"
                        :aria-expanded="expandedNodes.has(item.key)"
                        @click="toggleNode(item.key)"
                      >
                        <el-icon><ArrowRight /></el-icon>
                        <span>{{ item.first.allocation.node }}</span>
                        <span class="allocation-count">{{ item.rows.length }} 张卡</span>
                      </button>
                    </td>
                    <td role="cell" class="allocation-time">
                      <span aria-hidden="true" class="allocation-label">剩余时间</span>
                      <span :title="item.deadlines.length > 1 ? '各卡到期时间不同' : undefined">
                        {{ nodeRemaining(item) }}
                      </span>
                    </td>
                    <td v-if="session.identities.length > 1" role="cell">
                      <span aria-hidden="true" class="allocation-label">本人身份</span>
                      {{ item.first.group.identity?.subject }}
                    </td>
                    <td v-if="section.tasks" role="cell">
                      <span v-if="item.category" aria-hidden="true" class="allocation-label"
                        >用途</span
                      >
                      {{ item.category }}
                    </td>
                    <td v-if="section.metrics" role="cell" class="allocation-metrics"></td>
                  </tr>
                  <template v-if="item.rows.length === 1 || expandedNodes.has(item.key)">
                    <tr
                      v-for="row in item.rows"
                      :key="row.key"
                      role="row"
                      :class="{ 'allocation-detail': item.rows.length > 1 }"
                    >
                      <td role="cell" class="allocation-group">
                        {{ item.rows.length === 1 ? row.group.label : '' }}
                      </td>
                      <td role="cell" class="allocation-name">
                        <span v-if="item.rows.length === 1" class="allocation-node">{{
                          row.allocation.node ?? '未知节点'
                        }}</span>
                        <span v-if="row.allocation.device != null" class="allocation-device">
                          卡 {{ row.allocation.device }}
                        </span>
                      </td>
                      <td v-if="section.timeLabel" role="cell" class="allocation-time">
                        <template v-if="item.rows.length === 1 || item.deadlines.length > 1">
                          <span aria-hidden="true" class="allocation-label">{{
                            section.timeLabel
                          }}</span>
                          <time
                            v-if="Number.isFinite(expiration(row.allocation.expires_at))"
                            :datetime="
                              new Date(expiration(row.allocation.expires_at)).toISOString()
                            "
                            :title="date(expiration(row.allocation.expires_at) / 1000)"
                            >{{
                              section.holdings
                                ? remaining(row.allocation.expires_at)
                                : date(row.allocation.expires_at)
                            }}</time
                          >
                          <span v-else class="scope">未提供</span>
                        </template>
                      </td>
                      <td v-if="session.identities.length > 1" role="cell">
                        <template v-if="item.rows.length === 1">
                          <span aria-hidden="true" class="allocation-label">本人身份</span>
                          {{ row.group.identity?.subject }}
                        </template>
                      </td>
                      <td v-if="section.tasks" role="cell">
                        <span aria-hidden="true" class="allocation-label">用途</span>
                        <slot name="task" :row="row">
                          <el-popover
                            v-if="
                              row.allocation.own_task?.note || row.allocation.own_task?.agent_label
                            "
                            trigger="click"
                            placement="bottom-start"
                            :width="280"
                          >
                            <template #reference>
                              <button class="task-summary" type="button">
                                {{ row.allocation.own_task.category || '已登记' }}
                              </button>
                            </template>
                            <div class="task-detail">
                              <p v-if="row.allocation.own_task.note">
                                {{ row.allocation.own_task.note }}
                              </p>
                              <p v-if="row.allocation.own_task.agent_label" class="scope">
                                Agent · {{ row.allocation.own_task.agent_label }}
                              </p>
                            </div>
                          </el-popover>
                          <span v-else class="scope">{{
                            row.allocation.own_task?.category ||
                            (row.allocation.own_task === null ? '未登记' : '未提供')
                          }}</span>
                        </slot>
                      </td>
                      <td v-if="section.metrics" role="cell" class="allocation-metrics">
                        <span aria-hidden="true" class="allocation-label">利用率</span>
                        <slot v-if="fresh(row.group)" name="metrics" :row="row">
                          <ResourceMetricsCell
                            v-if="utilizationSnapshots[row.group.utilization?.connection]?.enabled"
                            v-bind="utilizationSnapshots[row.group.utilization.connection]"
                            :node-id="row.allocation.node"
                            :resource-id="
                              row.allocation.device == null
                                ? null
                                : `${row.allocation.node}:dev${row.allocation.device}`
                            "
                          />
                          <span v-else class="scope">未提供</span>
                        </slot>
                      </td>
                    </tr>
                  </template>
                </template>
              </tbody>
            </table>
            <p v-else class="empty">
              {{
                section.total
                  ? '没有符合筛选条件的记录。'
                  : resourceLoading
                    ? '正在读取…'
                    : resourcesReady
                      ? '已确认的群中暂无记录。'
                      : '资源信息待更新。'
              }}
            </p>
          </section>
        </template>
      </template>
      <template v-else-if="tab === 'mine'">
        <p v-if="resourceLoading" class="resource-loading" role="status">
          <template v-if="reportProgress.total">
            正在读取资源 · {{ reportProgress.done }} / {{ reportProgress.total }} 个来源
          </template>
          <template v-else>{{ resourcesReady ? '正在更新资源…' : '正在读取资源…' }}</template>
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
            <div v-else-if="item.value === null" class="stat-value stat-pending">暂无数据</div>
            <div v-else class="stat-value">
              <AnimatedResourceCount :value="item.value" /><span class="stat-unit">{{
                item.unit
              }}</span>
            </div>
          </div>
        </div>
        <p v-if="pendingGroups && !resourceLoading" class="resource-coverage" role="status">
          {{ pendingGroups }} 个资源群暂未返回有效数据
        </p>
        <div v-if="allocationRows.length || resourcesReady" class="workbench-focus">
          <section class="panel" aria-labelledby="workbench-expiring">
            <div class="workbench-section-heading">
              <h2 id="workbench-expiring" class="panel-title">即将到期</h2>
              <el-button v-if="hasHoldings" link type="primary" @click="openDetails('holdings')">
                查看全部持有
              </el-button>
            </div>
            <p v-if="!expiringItems.length" class="scope">
              {{
                resourceLoading
                  ? '正在检查…'
                  : pendingGroups
                    ? '已更新的资源暂无临期'
                    : '暂无 30 分钟内到期的资源'
              }}
            </p>
            <button
              v-for="item in expiringItems"
              :key="item.key"
              type="button"
              class="workbench-resource"
              @click="openWorkbenchResource(item)"
            >
              <span
                ><strong>{{ item.first.allocation.node ?? '未知节点' }}</strong
                ><small>{{ item.first.group.label }}</small></span
              >
              <span class="workbench-countdown">{{
                remaining(item.first.allocation.expires_at)
              }}</span>
            </button>
          </section>
          <section class="panel" aria-labelledby="workbench-bookings">
            <div class="workbench-section-heading">
              <h2 id="workbench-bookings" class="panel-title">我的预约</h2>
              <el-button
                v-if="recentBookings.length"
                link
                type="primary"
                @click="openDetails('reservations')"
              >
                查看全部预约
              </el-button>
            </div>
            <p v-if="!recentBookings.length" class="scope">
              {{
                resourceLoading
                  ? '正在读取…'
                  : pendingGroups
                    ? '已更新的资源群暂无预约'
                    : '暂无预约'
              }}
            </p>
            <button
              v-for="row in recentBookings"
              :key="row.key"
              type="button"
              class="workbench-resource"
              @click="
                openDetails('reservations', { resource: row.key, title: row.allocation.node })
              "
            >
              <span
                ><strong
                  >{{ row.allocation.node ?? '未知节点'
                  }}<span v-if="row.allocation.device != null" class="allocation-device"
                    >卡 {{ row.allocation.device }}</span
                  ></strong
                ><small>{{ row.group.label }}</small></span
              >
              <span v-if="Number.isInteger(row.allocation.position) && row.allocation.position >= 0"
                >第 {{ row.allocation.position + 1 }} 位</span
              >
              <span v-else class="scope">已预约</span>
            </button>
          </section>
        </div>
      </template>
      <div v-else-if="tab === 'groups'" class="group-grid">
        <section v-if="initialResourceLoading" class="group-card" role="status">
          <el-skeleton :rows="2" animated />
          <p class="group-meta">正在读取资源群…</p>
        </section>
        <section v-for="(group, index) in groups" :key="index" class="group-card">
          <div class="page-heading">
            <h2>{{ group.label }}</h2>
            <el-tag
              :type="fresh(group) ? 'success' : 'warning'"
              :title="
                Number.isFinite(expiration(group.source_at))
                  ? '状态采集于 ' + date(group.source_at)
                  : undefined
              "
              >{{ fresh(group) ? '已更新' : '待确认' }}</el-tag
            >
          </div>
          <p class="group-meta">{{ identityLabel(group.identity) }}</p>
          <div v-if="fresh(group)" class="group-metrics">
            <div v-for="item in groupCounts(group)" :key="item.label">
              <strong>{{ item.value }}</strong
              ><span>{{ item.label }}</span>
            </div>
          </div>
          <p v-else class="group-meta group-pending">本次未取得资源数据</p>
          <el-button
            v-if="fresh(group) && entries(group).length"
            text
            type="primary"
            @click="
              openDetails('group', { groupKey: allocationGroupKey(group), title: group.label })
            "
            >查看我的资源</el-button
          >
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
        <details
          v-for="(identity, index) in session.identities"
          :key="index"
          class="group-card connection-card"
        >
          <summary>已关联身份 · {{ identity.subject }}</summary>
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
        </details>
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
import {
  computed,
  nextTick,
  onMounted,
  onUnmounted,
  ref,
  shallowReactive,
  useSlots,
  watch,
} from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowRight } from '@element-plus/icons-vue'
import { pendingPanelTicket } from '../utils/personalPanelTicket'
import { formatResourceTime, parseResourceTime } from '../utils/resourceTime'
import LockbotWordmark from '../components/LockbotWordmark.vue'
import ResourceMetricsCell from '../components/ResourceMetricsCell.vue'
import ResourceUtilization from '../components/ResourceUtilization.vue'
import { datasourceIdentity } from '../utils/utilization/grants'
import { ProviderError } from '../utils/utilization/prometheus'
import AnimatedResourceCount from '../components/AnimatedResourceCount.vue'
import PersonalConnectionHandoff from '../components/PersonalConnectionHandoff.vue'
import PersonalCredentialView from '../components/PersonalCredentialView.vue'
import PersonalCredentialRotation from '../components/PersonalCredentialRotation.vue'

const tabs = [
  { id: 'mine', label: '我的占用' },
  { id: 'groups', label: '资源群' },
  { id: 'connections', label: '我的凭证' },
]
const route = useRoute()
const router = useRouter()
const pageTitle = ref(null)
const detailPage = computed(() => {
  const view = route.params.view
  return ['holdings', 'reservations', 'group'].includes(view) ? view : null
})
const tab = computed(() => {
  if (['groups', 'group'].includes(route.params.view)) return 'groups'
  return route.params.view === 'credentials' ? 'connections' : 'mine'
})
const detailSectionTitle = computed(() => {
  if (detailPage.value === 'reservations') return '预约与队列'
  return detailPage.value === 'group' ? '群内资源' : '持有资源'
})
const title = computed(() => {
  if (detailPage.value) return detail.value?.title || detailSectionTitle.value
  return tab.value === 'mine' ? '个人资源台' : tabs.find((item) => item.id === tab.value).label
})
function navigateTab(id) {
  return router.push({ mine: '/me', groups: '/me/groups', connections: '/me/credentials' }[id])
}
watch(
  () => route.path,
  async () => {
    await nextTick()
    pageTitle.value?.scrollIntoView({ block: 'start' })
    pageTitle.value?.focus({ preventScroll: true })
  }
)
const session = ref({ authenticated: false, identities: [] })
const groups = ref([])
const ticket = ref(null)
const combine = ref(false)
const error = ref('')
const busy = ref(false)
const initialized = ref(false)
const resourcesReady = ref(false)
const resourceLoading = ref(false)
const reportProgress = ref({ done: 0, total: 0 })
const initialResourceLoading = computed(
  () => resourceLoading.value && !resourcesReady.value && !groups.value.length
)
const knownGroups = computed(() => groups.value.filter(fresh))
const slots = useSlots()
const clock = ref(Date.now())
let clockTimer
const ownTasksAvailable = computed(() =>
  knownGroups.value.some((group) => entries(group).some((allocation) => 'own_task' in allocation))
)
const utilizationSnapshots = ref({})
const utilizationGroups = computed(() =>
  knownGroups.value
    .filter((group) => group.utilization?.configuration && group.utilization.connection)
    .map((group) => ({
      ...group.utilization,
      nodes: [
        ...new Set(
          entries(group)
            .filter((entry) => !queued(entry))
            .map((entry) => entry.node)
        ),
      ]
        .filter((node) => group.utilization.configuration.nodeMapping[node])
        .sort(),
    }))
    .filter(({ nodes }) => nodes.length)
)
watch(
  utilizationGroups,
  (active) => {
    const recognized = new Set(active.map(({ connection }) => connection))
    for (const id of Object.keys(utilizationSnapshots.value)) {
      if (!recognized.has(id)) delete utilizationSnapshots.value[id]
    }
  },
  { flush: 'sync' }
)
function updateUtilizationSnapshot(connection, snapshot) {
  if (utilizationGroups.value.some((item) => item.connection === connection)) {
    utilizationSnapshots.value[connection] = snapshot
  }
}
async function authorizeUtilization(connection, source, { signal }) {
  const result = await api('utilization/' + encodeURIComponent(connection), undefined, signal)
  if (
    !utilizationGroups.value.some((item) => item.connection === connection) ||
    datasourceIdentity(result.configuration) !== datasourceIdentity(source)
  )
    throw new ProviderError('grant')
  return result.access
}
const pendingGroups = computed(() => groups.value.length - knownGroups.value.length)
const allocationQuery = ref('')
const allocationGroup = ref('')
const allocationSort = ref('soonest')
// Route selections are scoped to this page session and contain no identity data in the URL.
const detailSelections = shallowReactive(new Map())
let detailSelectionId = 0
const detail = computed(() => {
  const type = detailPage.value
  const id = route.params.selection
  if (!type) return null
  if (!id) return type === 'group' ? null : { type }
  const selection = detailSelections.get(id)
  return selection?.type === type ? selection : null
})
watch(detail, (selection) => {
  allocationGroup.value = ''
  allocationQuery.value = ''
  allocationSort.value = 'soonest'
  expandedNodes.value.clear()
  if (selection?.type === 'holdings' && selection.resource)
    expandedNodes.value.add(selection.resource)
})
const expandedNodes = ref(new Set())
function toggleNode(key) {
  if (expandedNodes.value.has(key)) expandedNodes.value.delete(key)
  else expandedNodes.value.add(key)
}
watch(
  () => identityScope(session.value.identities),
  (_current, previous) => {
    if (previous === '[]' && !initialized.value) return
    detailSelections.clear()
    if (detailPage.value) router.replace(detailPage.value === 'group' ? '/me/groups' : '/me')
    expandedNodes.value.clear()
  },
  { flush: 'sync' }
)
const allocationOrder = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' })
function allocationGroupKey(group) {
  return JSON.stringify([group.identity, group.source, group.group_id ?? group.label])
}
const allocationRows = computed(() =>
  knownGroups.value.flatMap((group) => {
    const groupKey = allocationGroupKey(group)
    return entries(group).map((allocation, index) => ({
      group,
      groupKey,
      allocation,
      key: JSON.stringify([groupKey, allocation.kind, allocation.node, allocation.device, index]),
    }))
  })
)
const allocationGroups = computed(() =>
  [
    ...new Map(
      allocationRows.value.map(({ groupKey, group }) => [
        groupKey,
        { value: groupKey, label: group.label },
      ])
    ).values(),
  ].sort((a, b) => allocationOrder.compare(a.label, b.label))
)
watch(allocationGroups, (options) => {
  if (allocationGroup.value && !options.some((item) => item.value === allocationGroup.value)) {
    allocationGroup.value = ''
  }
})
function matchesAllocation({ group, groupKey, allocation }, query) {
  const selectedGroup = detail.value?.groupKey || allocationGroup.value
  if (selectedGroup && groupKey !== selectedGroup) return false
  const device = allocation.device
  const searchable = [
    group.label,
    allocation.node,
    allocation.own_task?.category,
    device == null ? '' : `dev${device} 卡 ${device}`,
  ]
    .join(' ')
    .toLocaleLowerCase()
  return query.every((term) => searchable.includes(term))
}
function sortAllocations(rows, holdings) {
  const compareResource = (a, b) =>
    allocationOrder.compare(a.allocation.node ?? '', b.allocation.node ?? '') ||
    allocationOrder.compare(a.allocation.device ?? '', b.allocation.device ?? '')
  const compareGroup = (a, b) => allocationOrder.compare(a.group.label, b.group.label)
  if (allocationSort.value === 'group')
    return rows.sort((a, b) => compareGroup(a, b) || compareResource(a, b))
  if (allocationSort.value === 'resource')
    return rows.sort((a, b) => compareResource(a, b) || compareGroup(a, b))
  if (!holdings) return rows
  return rows.sort((a, b) => {
    const left = expiration(a.allocation.expires_at)
    const right = expiration(b.allocation.expires_at)
    if (left === right) return compareGroup(a, b) || compareResource(a, b)
    if (!Number.isFinite(left)) return 1
    if (!Number.isFinite(right)) return -1
    return allocationSort.value === 'latest' ? right - left : left - right
  })
}
function allocationItems(rows, holdings) {
  const items = new Map()
  for (const row of rows) {
    const { node, device } = row.allocation
    const key =
      holdings && node != null && device != null ? JSON.stringify([row.groupKey, node]) : row.key
    if (!items.has(key)) items.set(key, { key, rows: [], first: row })
    const item = items.get(key)
    item.rows.push(row)
    const earliest = expiration(item.first.allocation.expires_at)
    const time = expiration(row.allocation.expires_at)
    if (Number.isFinite(time) && (!Number.isFinite(earliest) || time < earliest)) item.first = row
  }
  const byFirst = new Map([...items.values()].map((item) => [item.first, item]))
  return sortAllocations([...byFirst.keys()], holdings).map((first) => {
    const item = byFirst.get(first)
    const categories = new Set(item.rows.map((row) => row.allocation.own_task?.category || ''))
    const deadlines = [...new Set(item.rows.map((row) => expiration(row.allocation.expires_at)))]
    return {
      ...item,
      deadlines,
      category: categories.size === 1 ? categories.values().next().value : '',
    }
  })
}
function nodeRemaining({ deadlines }) {
  const known = deadlines.filter(Number.isFinite)
  if (!known.length) return '未提供'
  const first = remaining(Math.min(...known) / 1000)
  const last = remaining(Math.max(...known) / 1000)
  const range = first === last ? first : `${first} 至 ${last}`
  return known.length < deadlines.length ? `${range}（部分未知）` : range
}
const expiringItems = computed(() =>
  allocationItems(
    allocationRows.value.filter((row) => !queued(row.allocation)),
    true
  )
    .filter(({ first }) => {
      const timeLeft = expiration(first.allocation.expires_at) - clock.value
      return timeLeft > 0 && timeLeft <= 30 * 60_000
    })
    .sort(
      (a, b) =>
        expiration(a.first.allocation.expires_at) - expiration(b.first.allocation.expires_at)
    )
    .slice(0, 4)
)
const recentBookings = computed(() =>
  allocationRows.value.filter((row) => queued(row.allocation)).slice(0, 4)
)
const hasHoldings = computed(() => allocationRows.value.some((row) => !queued(row.allocation)))
function openDetails(type, selection = null) {
  if (!selection) return router.push(`/me/${type}`)
  const id = String(++detailSelectionId)
  detailSelections.set(id, { type, ...selection })
  return router.push(`/me/${type}/${id}`)
}
function openWorkbenchResource(item) {
  return openDetails('holdings', { resource: item.key, title: item.first.allocation.node })
}
const allocationSections = computed(() => {
  const query = allocationQuery.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  const holds = allocationRows.value.filter((row) => !queued(row.allocation))
  const reservations = allocationRows.value.filter((row) => queued(row.allocation))
  const matching = (rows, holdings) =>
    sortAllocations(
      rows.filter((row) => matchesAllocation(row, query)),
      holdings
    )
  return [
    {
      label: '持有资源',
      rows: matching(holds, true),
      total: holds.length,
      holdings: true,
      timeLabel: '剩余时间',
      tasks: Boolean(slots.task || ownTasksAvailable.value),
      metrics: Boolean(slots.metrics || utilizationGroups.value.length),
    },
    {
      label: '预约与队列',
      rows: matching(reservations, false),
      total: reservations.length,
      timeLabel: reservations.some(({ allocation }) =>
        Number.isFinite(expiration(allocation.expires_at))
      )
        ? '到期时间'
        : '',
    },
  ].map((section) => ({ ...section, items: allocationItems(section.rows, section.holdings) }))
})
const detailSections = computed(() => {
  const selection = detail.value
  if (!selection) return []
  const sections = allocationSections.value.filter(
    (section) =>
      selection.type === 'group' || Boolean(section.holdings) === (selection.type === 'holdings')
  )
  if (!selection.resource) return sections
  return sections.map((section) => {
    const items = section.items.filter((item) => item.key === selection.resource)
    const rows = items.flatMap((item) => item.rows)
    return { ...section, items, rows, total: rows.length }
  })
})
const detailUtilizationGroups = computed(() => {
  const selection = detail.value
  if (
    !selection ||
    ticket.value ||
    !session.value.authenticated ||
    selection.type === 'reservations'
  )
    return []
  const holdings = allocationRows.value.filter((row) => !queued(row.allocation))
  const selectedGroup = selection.groupKey || allocationGroup.value
  const nodesByConnection = new Map()
  for (const item of allocationItems(holdings, true)) {
    if (selectedGroup && item.first.groupKey !== selectedGroup) continue
    if (selection.resource && item.key !== selection.resource) continue
    const row = item.first
    const connection = row.group.utilization?.connection
    if (!connection) continue
    if (!nodesByConnection.has(connection)) nodesByConnection.set(connection, new Set())
    nodesByConnection.get(connection).add(row.allocation.node)
  }
  return utilizationGroups.value
    .map((group) => ({
      ...group,
      nodes: group.nodes.filter((node) => nodesByConnection.get(group.connection)?.has(node)),
    }))
    .filter(({ nodes }) => nodes.length)
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
      knownGroups.value.length || (resourcesReady.value && !groups.value.length) ? value : null,
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
function expiration(value) {
  const stamp = parseResourceTime(typeof value === 'number' ? value * 1000 : value).getTime()
  return Number.isFinite(stamp) ? stamp : Infinity
}
function remaining(value) {
  const stamp = expiration(value)
  if (!Number.isFinite(stamp)) return '未提供'
  const milliseconds = stamp - clock.value
  if (milliseconds <= 0) return '已到期'
  if (milliseconds < 60_000) return '不到 1 分钟'
  let minutes = Math.floor(milliseconds / 60_000)
  const days = Math.floor(minutes / 1440)
  minutes %= 1440
  const hours = Math.floor(minutes / 60)
  minutes %= 60
  return [days && `${days} 天`, hours && `${hours} 小时`, !days && minutes && `${minutes} 分钟`]
    .filter(Boolean)
    .join(' ')
}
function date(value) {
  if (value == null) return '未提供'
  return formatResourceTime(typeof value === 'number' ? value * 1000 : value)
}
function request(path, body, signal) {
  return fetch(`/api/personal-panel/${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    redirect: 'error',
    referrerPolicy: 'no-referrer',
    signal,
    ...(body === undefined
      ? {}
      : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  })
}
async function api(path, body, signal) {
  const response = await request(path, body, signal)
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
let reportRequest
function clearReports() {
  groups.value = []
  resourcesReady.value = false
}
function validateGroups(data) {
  if (!Array.isArray(data.groups)) throw new Error('资源响应无效，请稍后刷新。')
  return data.groups
}
async function readReports(signal, epoch) {
  const supportsStreaming = typeof TextDecoderStream === 'function'
  const response = await request(
    supportsStreaming ? 'resources-stream' : 'resources',
    undefined,
    signal
  )
  if (supportsStreaming && (response.status === 404 || response.status === 405)) {
    const data = await api('resources', undefined, signal)
    if (epoch === refreshEpoch) groups.value = validateGroups(data)
    return
  }
  if (!response.ok) throw new Error('无法更新资源或登录状态，请稍后刷新。')
  if (!response.headers?.get('content-type')?.includes('application/x-ndjson')) {
    const data = await response.json()
    if (epoch === refreshEpoch) groups.value = validateGroups(data)
    return
  }

  const batches = new Map()
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let remainder = ''
  let complete = false
  try {
    while (!complete) {
      const { done, value } = await reader.read()
      if (epoch !== refreshEpoch) return
      if (done) throw new Error('资源读取中断，请稍后刷新。')
      remainder += value
      let newline
      while ((newline = remainder.indexOf('\n')) !== -1) {
        const event = JSON.parse(remainder.slice(0, newline))
        remainder = remainder.slice(newline + 1)
        if (event.event === 'start') {
          if (reportProgress.value.total || !Number.isSafeInteger(event.total) || event.total < 1)
            throw new Error('资源响应无效，请稍后刷新。')
          reportProgress.value = { done: 0, total: event.total }
        } else if (
          event.event === 'batch' &&
          Number.isSafeInteger(event.index) &&
          event.index >= 0 &&
          event.index < reportProgress.value.total &&
          !batches.has(event.index)
        ) {
          batches.set(event.index, validateGroups(event))
          groups.value = [...batches]
            .sort(([left], [right]) => left - right)
            .flatMap(([, batch]) => batch)
          reportProgress.value = { done: batches.size, total: reportProgress.value.total }
        } else if (
          event.event === 'complete' &&
          reportProgress.value.total &&
          batches.size === reportProgress.value.total
        ) {
          complete = true
          break
        } else if (event.event === 'timeout') {
          if (!batches.size) clearReports()
          error.value = '部分来源响应超时，请刷新重试。'
          return false
        } else {
          clearReports()
          throw new Error('无法确认资源授权或读取结果，请稍后刷新。')
        }
      }
    }
  } finally {
    await reader.cancel().catch(() => {})
  }
}
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
  reportRequest?.abort()
  const controller = new AbortController()
  reportRequest = controller
  // Bound the first paint wait and a stalled response, including old servers without streaming.
  const deadline = setTimeout(() => controller.abort(), 18000)
  reportProgress.value = { done: 0, total: 0 }
  resourceLoading.value = true
  try {
    let current
    try {
      current = await api('session', undefined, controller.signal)
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
      const finished = await readReports(controller.signal, epoch)
      if (epoch === refreshEpoch) resourcesReady.value = finished !== false
    } catch (failure) {
      if (epoch === refreshEpoch) {
        groups.value = groups.value.map((group) => ({
          ...group,
          status: 'unknown',
          allocations: [],
        }))
        resourcesReady.value = false
      }
      throw failure
    }
  } catch (failure) {
    if (epoch !== refreshEpoch) return
    if (!initialized.value) clearReports()
    if (controller.signal.aborted) throw new Error('资源读取超时，请刷新重试。')
    throw failure
  } finally {
    clearTimeout(deadline)
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
  reportRequest?.abort()
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
onUnmounted(() => {
  clearInterval(clockTimer)
  ++refreshEpoch
  reportRequest?.abort()
})
onMounted(async () => {
  clockTimer = setInterval(() => (clock.value = Date.now()), 1000)
  await run(refresh)
  initialized.value = true
})
</script>

<style scoped src="../assets/styles/personal-panel.css"></style>

<style scoped>
.workbench-breadcrumb {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
  color: var(--muted);
  font-size: 13px;
}
.workbench-breadcrumb a {
  color: var(--accent);
  text-decoration: none;
}
.workbench-breadcrumb a:hover {
  text-decoration: underline;
}
.page-heading h1:focus {
  outline: none;
}
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
.stat-value .stat-unit {
  font-size: 14px;
}
.task-summary {
  background: none;
  border: 0;
  padding: 0;
  color: var(--accent);
}
.allocation-node {
  font-weight: 600;
}
.allocation-device {
  margin-inline-start: 8px;
  color: var(--muted);
  white-space: nowrap;
}
.resource-coverage {
  margin-top: 10px;
  color: var(--muted);
  font-size: 13px;
}
.workbench-section-heading {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  column-gap: 12px;
}
.allocation-controls {
  display: grid;
  grid-template-columns: minmax(200px, 300px) minmax(125px, 210px) 180px;
  gap: 8px;
  margin: 16px 0;
}
.allocation-controls-scoped {
  grid-template-columns: minmax(200px, 300px) 180px;
}
.allocation-controls :deep(.el-input__inner:focus-visible) {
  outline: none;
}
@media (max-width: 700px) {
  .allocation-controls {
    grid-template-columns: minmax(0, 1fr);
  }
  .allocation-controls > :first-child {
    grid-column: 1 / -1;
  }
}
.task-detail {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.group-pending {
  min-height: 48px;
  display: flex;
  align-items: center;
}
</style>
