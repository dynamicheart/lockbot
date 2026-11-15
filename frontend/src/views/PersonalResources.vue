<template>
  <div class="personal-panel">
    <header class="bar">
      <div class="bar-inner">
        <LockbotWordmark />
        <nav v-if="session.authenticated && !ticket" class="nav" aria-label="主导航">
          <router-link
            v-for="item in tabs"
            :key="item.path"
            :to="item.path"
            :class="{ active: tab === item.id }"
          >
            {{ item.label }}
          </router-link>
        </nav>
        <div v-if="!ticket" class="workbench-actions">
          <router-link to="/bots" class="workbench-console">机器人控制台</router-link>
          <PersonalWorkbenchPlugins
            v-if="session.authenticated"
            ref="pluginsDialog"
            :show-trigger="tab !== 'connections'"
            :inventory="session.workbench_plugins"
            :groups="groups"
            :metric-sources="utilizationGroups"
            :loading="resourceLoading"
          />
          <el-dropdown
            v-if="session.authenticated && tab === 'connections'"
            trigger="click"
            @command="handleCredentialAction"
          >
            <el-button>更多</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="plugins">已加载插件</el-dropdown-item>
                <el-dropdown-item divided command="clear" :disabled="busy">
                  清除此浏览器授权
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>
    </header>
    <main class="main" :class="{ 'credentials-page': tab === 'connections' && !ticket }">
      <nav
        v-if="detailPage && session.authenticated && !ticket"
        class="workbench-breadcrumb"
        aria-label="当前位置"
      >
        <router-link :to="detailPage === 'group' ? '/me/groups' : '/me'">
          {{ detailPage === 'group' ? '资源群' : '我的占用' }}
        </router-link>
        <el-icon aria-hidden="true"><ArrowRight /></el-icon>
        <template v-if="detail?.resource">
          <router-link :to="`/me/${detailPage}`">{{ detailSectionTitle }}</router-link>
          <el-icon aria-hidden="true"><ArrowRight /></el-icon>
        </template>
        <span aria-current="page">{{ detail?.title || detailSectionTitle }}</span>
      </nav>
      <div class="page-heading">
        <div class="workbench-heading-text">
          <h1 ref="pageTitle" tabindex="-1">{{ ticket ? '确认登录身份' : title }}</h1>
          <p v-if="detail?.resource && detail.groupLabel && !ticket" class="scope">
            {{ detail.groupLabel }}
          </p>
        </div>
        <div v-if="session.authenticated && !ticket && tab !== 'connections'" class="actions">
          <span v-if="resourceLoading && reportProgress.total" class="refresh-status" role="status">
            {{ reportProgress.done }} / {{ reportProgress.total }} 个资源群
          </span>
          <router-link
            v-else-if="pendingGroups && !resourceLoading && !detailPage && tab === 'mine'"
            class="refresh-status refresh-coverage"
            to="/me/groups"
          >
            {{ pendingGroups }} 个资源群未能更新
          </router-link>
          <el-button
            :icon="Refresh"
            :loading="resourceLoading"
            :disabled="busy"
            @click="run(refresh)"
          >
            {{ resourceLoading ? '更新中' : '刷新资源' }}
          </el-button>
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
      <section v-else-if="!session.authenticated" class="panel panel-onboarding">
        <h2>{{ $t('personalOnboarding.title') }}</h2>
        <figure class="panel-onboarding-flow">
          <img src="/workbench-onboarding.webp" alt="" width="1392" height="464" />
          <ol>
            <li>
              <strong>{{ $t('personalOnboarding.ask') }}</strong>
              <p>{{ $t('personalOnboarding.example') }}</p>
            </li>
            <li>
              <strong>{{ $t('personalOnboarding.receive') }}</strong>
              <p>{{ $t('personalOnboarding.receiveDetail') }}</p>
            </li>
            <li>
              <strong>{{ $t('personalOnboarding.open') }}</strong>
              <p>{{ $t('personalOnboarding.openDetail') }}</p>
            </li>
          </ol>
        </figure>
        <div class="panel-onboarding-actions">
          <el-button type="primary" @click="copyOnboardingRequest">
            {{ $t('personalOnboarding.copyRequest') }}
          </el-button>
          <span class="panel-onboarding-install">
            {{ $t('personalOnboarding.installHint') }}
            <a href="/api/agent-skill/lockbot-resource-agent.zip">
              {{ $t('personalOnboarding.installPackage') }}
            </a>
          </span>
        </div>
        <PersonalConnectionHandoff :authenticated="false" />
      </section>
      <template v-else-if="detailPage">
        <section v-if="!detail && resourceLoading" class="panel workbench-empty" role="status">
          <el-skeleton :rows="2" animated />
          <p class="scope">正在读取明细…</p>
        </section>
        <section v-else-if="!detail" class="panel workbench-empty">
          <p>
            {{
              !resourcesReady || pendingGroups
                ? '暂时无法确认这条明细，请刷新资源。'
                : '当前没有这条资源记录。'
            }}
          </p>
          <router-link :to="detailPage === 'group' ? '/me/groups' : '/me'">
            {{ detailPage === 'group' ? '返回资源群' : '返回我的占用' }}
          </router-link>
        </section>
        <template v-else>
          <ResourceUtilization
            v-for="{ connection, configuration, nodes, holdingsKey } in detailUtilizationGroups"
            :key="connection"
            :bot-id="connection"
            :scope-key="holdingsKey"
            :source="configuration"
            :nodes="nodes"
            :authorize="(source, options) => authorizeUtilization(connection, source, options)"
            :allowed="!ticket"
            hide-toolbar
            @change="updateUtilizationSnapshot(connection, holdingsKey, $event)"
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
              <el-option
                v-if="detailPage !== 'reservations'"
                label="即将到期优先"
                value="soonest"
              />
              <el-option
                v-if="detailPage !== 'reservations'"
                label="剩余时间最长优先"
                value="latest"
              />
              <el-option label="按资源群" value="group" />
              <el-option label="按节点 / 卡号" value="resource" />
            </el-select>
          </div>
          <p
            v-if="pendingGroups && !resourceLoading && !detail.groupKey && !detail.resource"
            class="resource-coverage"
            role="status"
          >
            {{ pendingGroups }} 个资源群未更新。<router-link to="/me/groups"
              >查看资源群</router-link
            >
          </p>
          <section v-for="section in detailSections" :key="section.label" class="panel">
            <h2 class="panel-title">
              {{ section.label }}<span class="detail-count">{{ section.rows.length }}</span>
            </h2>
            <table
              v-if="section.rows.length"
              class="allocation-table"
              :class="{ 'allocation-overview': section.holdings && !detail.resource }"
              role="table"
            >
              <thead role="rowgroup">
                <tr role="row">
                  <th
                    v-if="!detail.groupKey"
                    class="allocation-group"
                    scope="col"
                    role="columnheader"
                  >
                    资源群
                  </th>
                  <th class="allocation-name" scope="col" role="columnheader">资源</th>
                  <th v-if="section.timeLabel" scope="col" role="columnheader">
                    {{ section.timeLabel }}
                  </th>
                  <th v-if="session.identities.length > 1" scope="col" role="columnheader">
                    本人身份
                  </th>
                  <th v-if="!section.holdings" scope="col" role="columnheader">排队位置</th>
                  <th v-if="section.tasks" class="allocation-task" scope="col" role="columnheader">
                    用途
                  </th>
                  <th v-if="section.metrics && detail.resource" scope="col" role="columnheader">
                    利用率
                  </th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                <template v-for="item in section.items" :key="item.key">
                  <tr
                    v-if="section.holdings && !detail.resource"
                    role="row"
                    class="allocation-node-summary"
                    :class="{ 'allocation-node-open': expandedNodes.has(item.key) }"
                  >
                    <td v-if="!detail.groupKey" role="cell" class="allocation-group">
                      {{ item.first.group.label }}
                    </td>
                    <td role="cell" class="allocation-name">
                      <button
                        class="allocation-disclosure"
                        type="button"
                        :aria-expanded="expandedNodes.has(item.key)"
                        @click="toggleNode(item.key)"
                      >
                        <el-icon aria-hidden="true"><ArrowRight /></el-icon>
                        <span>{{ item.first.allocation.node ?? '未知节点' }}</span>
                        <span v-if="item.first.allocation.device != null" class="allocation-count">
                          {{ item.rows.length }} 张卡
                        </span>
                      </button>
                      <span
                        v-if="cardUtilization[item.key] != null"
                        class="allocation-card-summary"
                      >
                        卡平均利用率 {{ cardUtilization[item.key] }}%
                      </span>
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
                  </tr>
                  <template
                    v-if="detail.resource || !section.holdings || expandedNodes.has(item.key)"
                  >
                    <template v-for="row in item.rows" :key="row.key">
                      <tr
                        v-if="detail.resource || !section.holdings || hasHoldingDetails(row)"
                        role="row"
                        :class="{ 'allocation-detail': section.holdings && !detail.resource }"
                      >
                        <td v-if="!detail.groupKey" role="cell" class="allocation-group">
                          {{ section.holdings ? '' : row.group.label }}
                        </td>
                        <td role="cell" class="allocation-name">
                          <span
                            v-if="
                              !section.holdings ||
                              (detail.resource && row.allocation.device == null)
                            "
                            class="allocation-node"
                            >{{ row.allocation.node ?? '未知节点' }}</span
                          >
                          <span
                            v-if="
                              section.holdings && !detail.resource && row.allocation.device == null
                            "
                          >
                            整机
                          </span>
                          <span v-if="row.allocation.device != null" class="allocation-device">
                            卡 {{ row.allocation.device }}
                          </span>
                        </td>
                        <td v-if="section.timeLabel" role="cell" class="allocation-time">
                          <template
                            v-if="detail.resource || !section.holdings || item.deadlines.length > 1"
                          >
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
                          <template v-if="detail.resource || !section.holdings">
                            <span aria-hidden="true" class="allocation-label">本人身份</span>
                            {{ row.group.identity?.subject }}
                          </template>
                        </td>
                        <td v-if="!section.holdings" role="cell">
                          <span aria-hidden="true" class="allocation-label">排队位置</span>
                          {{ bookingPosition(row.allocation) }}
                        </td>
                        <td v-if="section.tasks" role="cell">
                          <span aria-hidden="true" class="allocation-label">用途</span>
                          <slot name="task" :row="row">
                            <el-popover
                              v-if="
                                row.allocation.own_task?.note ||
                                row.allocation.own_task?.agent_label
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
                        <td
                          v-if="section.metrics && detail.resource"
                          role="cell"
                          class="allocation-metrics"
                          :class="{ 'allocation-device-metrics': row.allocation.device != null }"
                        >
                          <template v-if="detail.resource || row.allocation.device != null">
                            <span aria-hidden="true" class="allocation-label">利用率</span>
                            <slot v-if="fresh(row.group)" name="metrics" :row="row">
                              <ResourceMetricsCell
                                v-if="
                                  utilizationSnapshots[row.group.utilization?.connection]?.enabled
                                "
                                v-bind="utilizationSnapshots[row.group.utilization.connection]"
                                :now="clock"
                                :node-id="row.allocation.node"
                                :resource-id="
                                  row.allocation.device == null
                                    ? null
                                    : `${row.allocation.node}:dev${row.allocation.device}`
                                "
                              />
                              <span v-else class="scope">未提供</span>
                            </slot>
                          </template>
                        </td>
                      </tr>
                    </template>
                    <tr
                      v-if="
                        section.holdings &&
                        (holdingAddress(item) ||
                          holdingGuidance(item).length ||
                          (!detail.resource &&
                            (slots.metrics ||
                              item.first.group.utilization ||
                              !item.rows.some(hasHoldingDetails))))
                      "
                      class="allocation-detail"
                      role="row"
                    >
                      <td
                        :colspan="
                          2 +
                          Number(!detail.groupKey) +
                          Number(section.tasks) +
                          Number(session.identities.length > 1) +
                          Number(section.metrics && !!detail.resource)
                        "
                        class="allocation-inline-metrics"
                        role="cell"
                      >
                        <div v-if="holdingAddress(item)" class="node-access-details">
                          <span>{{ $t('nodeAccess.loginAddress') }}</span>
                          <code>{{ holdingAddress(item) }}</code>
                        </div>
                        <div v-if="holdingGuidance(item).length" class="node-access-details">
                          <button
                            v-for="reference in holdingGuidance(item)"
                            :key="reference.id"
                            type="button"
                            class="task-summary"
                            @click="openGuidance(reference)"
                          >
                            {{ holdingGuidance(item).length === 1 ? '运维说明' : reference.title }}
                          </button>
                        </div>
                        <template v-for="row in detail.resource ? [] : item.rows" :key="row.key">
                          <slot name="metrics" :row="row">
                            <ResourceMetricsCell
                              v-if="
                                utilizationSnapshots[row.group.utilization?.connection]?.enabled
                              "
                              v-bind="utilizationSnapshots[row.group.utilization.connection]"
                              :now="clock"
                              :node-id="row.allocation.node"
                              :resource-id="
                                row.allocation.device == null
                                  ? null
                                  : `${row.allocation.node}:dev${row.allocation.device}`
                              "
                            />
                          </slot>
                        </template>
                        <span
                          v-if="
                            !detail.resource &&
                            !holdingAddress(item) &&
                            !holdingGuidance(item).length &&
                            !slots.metrics &&
                            !utilizationSnapshots[item.first.group.utilization?.connection]?.enabled
                          "
                          class="scope"
                          >{{
                            item.first.group.utilization ? '暂无利用率数据' : '暂无更多资源详情'
                          }}</span
                        >
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
                    : detail.groupKey &&
                        !knownGroups.some((group) => allocationGroupKey(group) === detail.groupKey)
                      ? '暂未取得该群的资源数据。'
                      : resourcesReady
                        ? section.holdings
                          ? '暂无持有资源'
                          : '暂无预约'
                        : '暂未取得资源数据。'
              }}
            </p>
          </section>
        </template>
      </template>
      <template v-else-if="tab === 'mine'">
        <div class="workbench-stats">
          <component
            :is="item.value > 0 ? 'RouterLink' : 'div'"
            v-for="item in statistics"
            :key="item.label"
            :to="item.value > 0 ? item.path : undefined"
            class="workbench-stat"
          >
            <span class="workbench-stat-heading">
              <el-icon class="workbench-stat-icon" aria-hidden="true"
                ><component :is="item.icon"
              /></el-icon>
              <span>{{ item.label }}</span>
              <el-icon v-if="item.value > 0" class="workbench-stat-next" aria-hidden="true"
                ><ArrowRight
              /></el-icon>
            </span>
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
            <span v-else-if="item.value === null" class="stat-value stat-pending">暂无数据</span>
            <span v-else class="stat-value">
              <AnimatedResourceCount :value="item.value" /><span class="stat-unit">{{
                item.unit
              }}</span>
            </span>
          </component>
        </div>
        <div v-if="allocationRows.length || resourcesReady" class="workbench-focus">
          <section class="panel" aria-labelledby="workbench-expiring">
            <div class="workbench-panel-heading">
              <h2 id="workbench-expiring" class="panel-title">即将到期</h2>
              <router-link v-if="expiringItems.length > 4" to="/me/holdings">
                全部持有
              </router-link>
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
            <component
              :is="holdingPath(item) ? 'RouterLink' : 'button'"
              v-for="item in expiringItems.slice(0, 4)"
              :key="item.key"
              :to="holdingPath(item)"
              :type="holdingPath(item) ? undefined : 'button'"
              class="workbench-resource"
              @click="!holdingPath(item) && openWorkbenchResource(item)"
            >
              <span
                ><strong>{{ item.first.allocation.node ?? '未知节点' }}</strong
                ><small>
                  {{ item.first.group.label }}
                  <template v-if="item.endingCards > 1">
                    · {{ item.endingCards }} 张卡临期</template
                  >
                  <template v-else-if="item.first.allocation.device != null">
                    · 卡 {{ item.first.allocation.device }}
                  </template>
                </small></span
              >
              <span class="workbench-row-status">
                <span class="workbench-countdown">{{
                  remaining(item.first.allocation.expires_at)
                }}</span>
                <el-icon aria-hidden="true"><ArrowRight /></el-icon>
              </span>
            </component>
          </section>
          <section class="panel" aria-labelledby="workbench-bookings">
            <div class="workbench-panel-heading">
              <h2 id="workbench-bookings" class="panel-title">我的预约</h2>
              <router-link v-if="recentBookings.length > 4" to="/me/reservations">
                全部预约
              </router-link>
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
            <component
              :is="row.allocation.detail_id ? 'RouterLink' : 'button'"
              v-for="row in recentBookings.slice(0, 4)"
              :key="row.key"
              :to="detailPath('reservations', row.allocation.detail_id)"
              :type="row.allocation.detail_id ? undefined : 'button'"
              class="workbench-resource"
              @click="!row.allocation.detail_id && openBooking(row)"
            >
              <span
                ><strong
                  >{{ row.allocation.node ?? '未知节点'
                  }}<span v-if="row.allocation.device != null" class="allocation-device"
                    >卡 {{ row.allocation.device }}</span
                  ></strong
                ><small>{{ row.group.label }}</small></span
              >
              <span class="workbench-row-status">
                <span>{{ bookingPosition(row.allocation) }}</span>
                <el-icon aria-hidden="true"><ArrowRight /></el-icon>
              </span>
            </component>
          </section>
        </div>
      </template>
      <div v-else-if="tab === 'groups'" class="group-grid">
        <section v-if="initialResourceLoading" class="group-card" role="status">
          <el-skeleton :rows="2" animated />
          <p class="group-meta">正在读取资源群…</p>
        </section>
        <section v-for="group in groups" :key="allocationGroupKey(group)" class="group-card">
          <div class="page-heading">
            <h2>{{ group.label }}</h2>
            <el-tag
              :type="fresh(group) ? 'success' : 'warning'"
              :title="
                Number.isFinite(expiration(group.source_at))
                  ? '状态采集于 ' + date(group.source_at)
                  : undefined
              "
              >{{ fresh(group) ? '已更新' : '暂不可用' }}</el-tag
            >
          </div>
          <p class="group-meta">{{ identityLabel(group.identity) }}</p>
          <div v-if="fresh(group)" class="group-metrics">
            <div v-for="item in groupCounts(group)" :key="item.label">
              <strong
                >{{ item.value
                }}<small v-if="item.total != null"> / {{ item.total }}</small></strong
              >
              <span>{{ item.label }}</span>
            </div>
          </div>
          <p v-else class="group-meta group-pending">本次未取得资源数据</p>
          <el-button v-if="fresh(group)" text type="primary" @click="openGroup(group)"
            >查看群内明细</el-button
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
        <details class="credential-access-help">
          <summary>如何添加其他机器群的 Key</summary>
          <p class="scope">
            在已绑定该群的 Agent 中说“打开我的资源台”，打开链接后确认添加。
            <a href="/api/agent-skill/SKILL.md" target="_blank" rel="noopener noreferrer"
              >Agent 使用说明</a
            >
          </p>
        </details>
      </template>
    </main>
    <el-dialog
      :model-value="Boolean(guidanceDialog)"
      :title="guidanceDialog?.title || '运维说明'"
      width="min(460px, calc(100vw - 32px))"
      align-center
      @close="clearGuidance"
    >
      <p v-if="guidanceDialog?.loading" aria-live="polite">正在读取运维说明…</p>
      <p v-else-if="guidanceDialog?.error" role="alert">运维说明暂不可用，请刷新资源后重试。</p>
      <template v-else-if="guidanceDialog">
        <p class="guidance-summary">{{ guidanceDialog.summary }}</p>
        <a
          v-if="guidanceDialog.url"
          :href="guidanceDialog.url"
          target="_blank"
          rel="noopener noreferrer"
          >打开运维文档</a
        >
      </template>
    </el-dialog>
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
import { ElMessageBox } from 'element-plus'
import { ArrowRight, Calendar, Cpu, Monitor, Refresh } from '@element-plus/icons-vue'
import { pendingPanelTicket } from '../utils/personalPanelTicket'
import { formatNodeAccess } from '../utils/nodeAccess'
import { useHelpers } from '../utils/helpers'
import { useI18n } from 'vue-i18n'
import { formatResourceTime, parseResourceTime } from '../utils/resourceTime'
import LockbotWordmark from '../components/LockbotWordmark.vue'
import PersonalWorkbenchPlugins from '../components/PersonalWorkbenchPlugins.vue'
import ResourceMetricsCell from '../components/ResourceMetricsCell.vue'
import ResourceUtilization from '../components/ResourceUtilization.vue'
import { datasourceIdentity } from '../utils/utilization/grants'
import { metricStatus, ProviderError, resolveCards } from '../utils/utilization/prometheus'
import AnimatedResourceCount from '../components/AnimatedResourceCount.vue'
import PersonalConnectionHandoff from '../components/PersonalConnectionHandoff.vue'
import PersonalCredentialView from '../components/PersonalCredentialView.vue'
import PersonalCredentialRotation from '../components/PersonalCredentialRotation.vue'

const tabs = [
  { id: 'mine', label: '我的占用', path: '/me' },
  { id: 'groups', label: '资源群', path: '/me/groups' },
  { id: 'connections', label: '我的凭证', path: '/me/credentials' },
]
const { copyText } = useHelpers()
const { t } = useI18n()
function copyOnboardingRequest() {
  copyText(t('personalOnboarding.request', { origin: window.location.origin }))
}
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
const guidanceDialog = ref(null)
let guidanceRead = null
const pluginsDialog = ref(null)
function handleCredentialAction(command) {
  if (command === 'plugins') pluginsDialog.value?.open()
  if (command === 'clear') run(confirmLogout)
}
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
const holdingCutoff = computed(() => {
  // Recompute derived lists and metric scopes only when a holding crosses its deadline.
  let cutoff = -Infinity
  for (const group of knownGroups.value) {
    for (const allocation of Array.isArray(group.allocations) ? group.allocations : []) {
      if (queued(allocation)) continue
      const deadline = expiration(allocation.expires_at)
      if (deadline <= clock.value && deadline > cutoff) cutoff = deadline
    }
  }
  return cutoff
})
let clockTimer
const ownTasksAvailable = computed(() =>
  knownGroups.value.some((group) => entries(group).some((allocation) => 'own_task' in allocation))
)
const utilizationSnapshots = ref({})
const utilizationGroups = computed(() =>
  knownGroups.value
    .filter((group) => group.utilization?.configuration && group.utilization.connection)
    .map((group) => {
      const holdings = entries(group).filter(
        (entry) => !queued(entry) && group.utilization.configuration.nodeMapping[entry.node]
      )
      return {
        ...group.utilization,
        nodes: [...new Set(holdings.map(({ node }) => node))].sort(),
        holdingsKey: JSON.stringify(
          holdings
            .map(({ node, device, expires_at }) => JSON.stringify([node, device, expires_at]))
            .sort()
        ),
      }
    })
    .filter(({ nodes }) => nodes.length)
)
watch(
  utilizationGroups,
  (active) => {
    const recognized = new Map(
      active.map(({ connection, holdingsKey }) => [connection, holdingsKey])
    )
    for (const id of Object.keys(utilizationSnapshots.value)) {
      if (!recognized.has(id) || recognized.get(id) !== utilizationSnapshots.value[id].holdingsKey)
        delete utilizationSnapshots.value[id]
    }
  },
  { flush: 'sync' }
)
function updateUtilizationSnapshot(connection, holdingsKey, snapshot) {
  const group = utilizationGroups.value.find((item) => item.connection === connection)
  if (group?.holdingsKey === holdingsKey) {
    utilizationSnapshots.value[connection] = { ...snapshot, holdingsKey }
  }
}
async function authorizeUtilization(connection, source, { signal }) {
  const result = await api('utilization/' + encodeURIComponent(connection), undefined, signal)
  if (
    !utilizationGroups.value.some((item) => item.connection === connection) ||
    datasourceIdentity(result.configuration) !== datasourceIdentity(source) ||
    result.access?.scope_version !== 2 ||
    !Array.isArray(result.access.node_ids)
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
  if (selection?.type === type) return selection
  if (type === 'group') {
    const group = groups.value.find((item) => item.detail_id === id)
    return group ? groupSelection(group) : null
  }
  const row = allocationRows.value.find(({ allocation }) =>
    type === 'reservations'
      ? queued(allocation) && allocation.detail_id === id
      : !queued(allocation) &&
        (allocation.device == null ? allocation.detail_id : allocation.node_detail_id) === id
  )
  return row ? resourceSelection(type, row) : null
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
  return (
    group.detail_id || JSON.stringify([group.identity, group.source, group.group_id ?? group.label])
  )
}
const allocationRows = computed(() =>
  knownGroups.value.flatMap((group) => {
    const groupKey = allocationGroupKey(group)
    return entries(group).map((allocation, index) => ({
      group,
      groupKey,
      allocation,
      key:
        allocation.detail_id ||
        JSON.stringify([groupKey, allocation.kind, allocation.node, allocation.device, index]),
    }))
  })
)
watch(
  () => JSON.stringify([detail.value?.type, detail.value?.resource, detail.value?.groupKey]),
  () => {
    const selection = detail.value
    allocationGroup.value = ''
    allocationQuery.value = ''
    allocationSort.value = selection?.type === 'reservations' ? 'resource' : 'soonest'
    expandedNodes.value.clear()
  }
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
function inDetailGroup(row) {
  return !detail.value?.groupKey || row.groupKey === detail.value.groupKey
}
function matchesAllocation(row, query) {
  const { group, groupKey, allocation } = row
  if (!inDetailGroup(row) || (allocationGroup.value && groupKey !== allocationGroup.value))
    return false
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
function hasHoldingDetails({ allocation }) {
  return allocation.device != null || allocation.own_task?.note || allocation.own_task?.agent_label
}
function holdingAddress({ rows }) {
  const row = rows.find(({ allocation }) => {
    const expiry = expiration(allocation.expires_at)
    return allocation.node_access && Number.isFinite(expiry) && expiry > clock.value
  })
  return row ? formatNodeAccess(row.allocation.node_access) : ''
}
function holdingGuidance({ rows }) {
  const references = new Map()
  for (const { allocation } of rows) {
    const expiresAt = expiration(allocation.expires_at)
    if (!Number.isFinite(expiresAt) || expiresAt <= clock.value) continue
    if (!Array.isArray(allocation.resource_guidance)) continue
    for (const item of allocation.resource_guidance) {
      if (item?.protocol === 'lockbot.resource-guidance.v1' && /^[a-f0-9]{32}$/.test(item.id)) {
        references.set(item.id, { ...item, expiresAt })
      }
    }
  }
  return [...references.values()]
}
function clearGuidance() {
  guidanceRead?.abort()
  guidanceRead = null
  guidanceDialog.value = null
}
async function openGuidance(reference) {
  clearGuidance()
  const controller = new AbortController()
  guidanceRead = controller
  guidanceDialog.value = { title: reference.title, expiresAt: reference.expiresAt, loading: true }
  try {
    const result = await api(`resource-guidance/${reference.id}`, undefined, controller.signal)
    if (guidanceRead !== controller) return
    if (reference.expiresAt <= Date.now()) {
      clearGuidance()
      return
    }
    if (
      result.protocol !== reference.protocol ||
      result.id !== reference.id ||
      result.revision !== reference.revision ||
      typeof result.summary !== 'string'
    )
      throw new Error('Reference changed')
    const url = result.documentation_url ? new URL(result.documentation_url) : null
    guidanceDialog.value = {
      title: reference.title,
      expiresAt: reference.expiresAt,
      summary: result.summary,
      url: url?.protocol === 'https:' && !url.username && !url.password ? url.href : null,
    }
  } catch {
    if (guidanceRead === controller)
      guidanceDialog.value = { title: reference.title, expiresAt: reference.expiresAt, error: true }
  }
}
watch([() => route.fullPath, () => session.value.authenticated, groups], clearGuidance)
watch(clock, (now) => {
  if (guidanceDialog.value?.expiresAt <= now) clearGuidance()
})
onUnmounted(clearGuidance)
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
    allocationRows.value.filter(({ allocation }) => {
      const timeLeft = expiration(allocation.expires_at) - clock.value
      return !queued(allocation) && timeLeft > 0 && timeLeft <= 30 * 60_000
    }),
    true
  )
    .sort(
      (a, b) =>
        expiration(a.first.allocation.expires_at) - expiration(b.first.allocation.expires_at)
    )
    .map((item) => ({
      ...item,
      endingCards: item.rows.filter(({ allocation }) => allocation.device != null).length,
    }))
)
const recentBookings = computed(() =>
  allocationRows.value
    .filter((row) => queued(row.allocation))
    .sort(
      (a, b) =>
        allocationOrder.compare(a.group.label, b.group.label) ||
        allocationOrder.compare(a.allocation.node ?? '', b.allocation.node ?? '') ||
        allocationOrder.compare(a.allocation.device ?? '', b.allocation.device ?? '')
    )
)
function groupSelection(group) {
  return { type: 'group', groupKey: allocationGroupKey(group), title: group.label }
}
function resourceSelection(type, row) {
  const { node, device } = row.allocation
  const resource =
    type === 'holdings' && node != null && device != null
      ? JSON.stringify([row.groupKey, node])
      : row.key
  return { type, groupKey: row.groupKey, resource, title: node, groupLabel: row.group.label }
}
function detailPath(type, id) {
  return id ? `/me/${type}/${encodeURIComponent(id)}` : undefined
}
function holdingPath({ first: { allocation } }) {
  const selector = allocation.device == null ? allocation.detail_id : allocation.node_detail_id
  return selector ? { path: '/me/holdings', query: { node: selector } } : undefined
}
function openDetails(selection, id) {
  if (!id) {
    id = String(++detailSelectionId)
    detailSelections.set(id, selection)
  }
  return router.push(detailPath(selection.type, id))
}
function openGroup(group) {
  return openDetails(groupSelection(group), group.detail_id)
}
function openBooking(row) {
  return openDetails(resourceSelection('reservations', row), row.allocation.detail_id)
}
function openWorkbenchResource({ first }) {
  const { device, detail_id: id, node_detail_id: nodeId } = first.allocation
  let selector = device == null ? id : nodeId
  if (!selector) {
    selector = String(++detailSelectionId)
    detailSelections.set(selector, resourceSelection('holdings', first))
  }
  return router.push({ path: '/me/holdings', query: { node: selector } })
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
      total: holds.filter(inDetailGroup).length,
      holdings: true,
      timeLabel: '剩余时间',
      tasks: Boolean(slots.task || ownTasksAvailable.value),
      metrics: Boolean(slots.metrics || utilizationGroups.value.length),
    },
    {
      label: '预约与队列',
      rows: matching(reservations, false),
      total: reservations.filter(inDetailGroup).length,
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
const selectedHolding = computed(() => {
  const selector = route.query.node
  if (detailPage.value !== 'holdings' || !selector) return
  const cached = detailSelections.get(selector)?.resource
  return detailSections.value[0]?.items.find(({ key, first: { allocation } }) =>
    cached
      ? key === cached
      : (allocation.device == null ? allocation.detail_id : allocation.node_detail_id) === selector
  )?.key
})
watch(
  selectedHolding,
  (key) => {
    if (key) expandedNodes.value.add(key)
  },
  { flush: 'post' }
)

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
    if (!selection.resource && !showsUtilizationSummary(item) && !expandedNodes.value.has(item.key))
      continue
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
function showsUtilizationSummary(item) {
  return item.first.allocation.device == null || item.rows.length > 1
}
const cardUtilization = computed(() => {
  const summaries = Object.create(null)
  for (const section of detailSections.value) {
    if (!section.holdings) continue
    for (const item of section.items) {
      if (!showsUtilizationSummary(item) || expandedNodes.value.has(item.key)) continue
      const source = utilizationSnapshots.value[item.first.group.utilization?.connection]
      if (!source?.enabled || !source.snapshot) continue
      const wholeNode = item.first.allocation.device == null
      const samples = new Map()
      for (const { allocation } of item.rows) {
        const expires = expiration(allocation.expires_at)
        if (!Number.isFinite(expires) || expires <= clock.value) continue
        const { node, device } = allocation
        const cards = resolveCards(
          source.snapshot,
          source.config,
          node,
          wholeNode ? null : `${node}:dev${device}`
        )
        for (const card of cards) samples.set(card.device, card.metrics.compute)
      }
      if (!samples.size || (!wholeNode && samples.size !== item.rows.length)) continue
      const values = [...samples.values()]
      if (
        values.some(
          (sample) => metricStatus(sample, clock.value, source.config.staleSeconds) !== 'ok'
        )
      )
        continue
      const total = values.reduce((sum, sample) => sum + sample.value, 0)
      summaries[item.key] = Math.round(total / samples.size)
    }
  }
  return summaries
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
    ['持有机器', machines, '台', Monitor, '/me/holdings'],
    ['持有卡', cards, '张', Cpu, '/me/holdings'],
    ['预约', bookings, '项', Calendar, '/me/reservations'],
  ].map(([label, value, unit, icon, path]) => ({
    label,
    icon,
    path,
    value:
      knownGroups.value.length || (resourcesReady.value && !groups.value.length) ? value : null,
    unit,
  }))
})
function fresh(group) {
  return ['ok', 'fresh', 'active', 'ready', 'available', 'healthy'].includes(group.status)
}
function entries(group) {
  return Array.isArray(group.allocations)
    ? group.allocations.filter(
        (allocation) =>
          queued(allocation) || expiration(allocation.expires_at) > holdingCutoff.value
      )
    : []
}
function bookingPosition(allocation) {
  const { position } = allocation
  return Number.isInteger(position) && position >= 0 ? `第 ${position + 1} 位` : '已预约'
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
function groupCounts(group) {
  if (!fresh(group)) return []
  const holdings = entries(group).filter((entry) => !queued(entry))
  const nodes = new Set(holdings.filter(({ node }) => node != null).map(({ node }) => node))
  const cards = new Set(
    holdings
      .filter(({ node, device }) => node != null && device != null)
      .map(({ node, device }) => JSON.stringify([node, device]))
  )
  const total = (value, owned) => (Number.isSafeInteger(value) && value >= owned ? value : null)
  const nodeTotal = total(group.capacity?.nodes, nodes.size)
  const cardTotal = total(group.capacity?.cards, cards.size)
  return [
    {
      label: nodeTotal == null ? '我的机器（台）' : '机器（我的 / 全群）',
      value: nodes.size,
      total: nodeTotal,
    },
    ...(cardTotal != null || cards.size
      ? [
          {
            label: cardTotal == null ? '我的卡（张）' : '卡（我的 / 全群）',
            value: cards.size,
            total: cardTotal,
          },
        ]
      : []),
    { label: '我的预约', value: entries(group).filter(queued).length },
  ]
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
async function confirmLogout() {
  try {
    await ElMessageBox.confirm(
      '清除后，此浏览器需要重新授权才能查看个人资源。Agent 的 Key 仍然有效。',
      '清除此浏览器授权？',
      {
        cancelButtonText: '取消',
        confirmButtonText: '清除授权',
        confirmButtonType: 'danger',
        type: 'warning',
        closeOnClickModal: false,
      }
    )
  } catch (action) {
    if (action === 'cancel' || action === 'close') return
    throw action
  }
  await logout()
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
.guidance-summary {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.node-access-details {
  color: var(--lb-text-secondary);
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
}
.node-access-details code {
  overflow-wrap: anywhere;
}
.workbench-heading-text {
  min-width: 0;
  overflow-wrap: anywhere;
}
.workbench-heading-text .scope {
  margin: 4px 0 0;
}
.workbench-breadcrumb {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
  overflow-wrap: anywhere;
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
  width: min(88px, 100%);
  margin-top: 8px;
}
.stat-skeleton {
  width: min(88px, 100%);
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
  margin-block: 10px 0;
  color: var(--muted);
  font-size: 13px;
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
