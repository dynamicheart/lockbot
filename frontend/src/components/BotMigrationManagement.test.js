/* eslint-disable vue/one-component-per-file -- Slot stubs retain the actual migration SFC/controller. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import {
  createRenderer,
  createSSRApp,
  defineComponent,
  h,
  nextTick,
  reactive,
  ssrContextKey,
} from 'vue'
import { createI18n } from 'vue-i18n'
import { renderToString } from 'vue/server-renderer'
import en from '../i18n/en'
import zh from '../i18n/zh-CN'
import BotMigrationManagement from './BotMigrationManagement.vue'
import { migrationStatus } from '../utils/useMigrationManagement'
import api from '../utils/api'
import { ElMessageBox } from 'element-plus'
vi.mock('../utils/api', () => ({ default: { get: vi.fn(), post: vi.fn() } }))
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn() },
  ElMessageBox: { confirm: vi.fn() },
}))
const renderer = createRenderer({
  createElement: (tag) => ({ tag, children: [], props: {} }),
  createText: (text) => ({ text }),
  createComment: () => ({ text: '' }),
  insert(child, parent, anchor) {
    child.parent = parent
    const i = parent.children.indexOf(anchor)
    parent.children.splice(i < 0 ? parent.children.length : i, 0, child)
  },
  remove(child) {
    const i = child.parent?.children.indexOf(child)
    if (i >= 0) child.parent.children.splice(i, 1)
  },
  setText: (node, text) => (node.text = text),
  setElementText(node, text) {
    node.text = text
    node.children = []
  },
  parentNode: (node) => node.parent,
  nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1],
  patchProp: (node, key, old, value) => (node.props[key] = value),
})
const Container = defineComponent({
  setup:
    (_, { slots, attrs }) =>
    () =>
      h('div', attrs, [slots.label?.(), slots.default?.(), slots.footer?.()]),
})
const flush = async () => {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}
let app, state, props, root, gets, posts
const id = '1'.repeat(32),
  digest = 'a'.repeat(64)
const resetCounts = {
  holders: 2,
  reservations: 3,
  offers: 1,
  agent_bindings: 4,
  declarations: 2,
  op_keys: 1,
  identity_whitelist_users: 2,
}
const status = (extra = {}) => ({
  operation_id: id,
  bot_id: 4,
  phase: 'planned',
  commands_replayed: false,
  active_state_migration_supported: false,
  source_revision: 2,
  target_revision: 3,
  target_digest: digest,
  target_application_id: 7,
  target_application_name: 'Target app',
  target_chat_id: 'oc_target',
  next_action: 'resume',
  can_resume: true,
  reset_counts: { ...resetCounts },
  reset_applied: false,
  can_abort: true,
  ...extra,
})
const cap = (extra = {}) => ({
  enabled: true,
  supported_source: true,
  reset_preview: { digest: digest, counts: { ...resetCounts } },
  source_uuid: 'fixture-uuid',
  identity_revision: 2,
  latest_operation: null,
  ...extra,
})
function deferred(list, path, body, options) {
  return new Promise((resolve, reject) => list.push({ path, body, options, resolve, reject }))
}
function mount(extra = {}) {
  props = reactive({
    botId: 4,
    botUuid: 'fixture-uuid',
    identityRevision: 2,
    authorized: true,
    authScope: 'admin-a',
    ...extra,
  })
  const Host = defineComponent({ setup: () => () => h(BotMigrationManagement, props) })
  root = { children: [] }
  app = renderer.createApp(Host)
  app.provide(ssrContextKey, { modules: new Set() })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en, 'zh-CN': zh } }))
  app.config.warnHandler = () => {}
  for (const name of [
    'ElDialog',
    'ElForm',
    'ElFormItem',
    'ElSelect',
    'ElOption',
    'ElInput',
    'ElTooltip',
    'ElIcon',
    'ElButton',
    'ElCheckbox',
  ])
    app.component(name, Container)
  app.mount(root)
  state = app._instance.subTree.component.setupState
}
async function ready() {
  gets[0].resolve({ data: cap() })
  await flush()
  gets[1].resolve({ data: [{ id: 7, name: 'Target app', app_secret: 'NEVER_RETAIN' }] })
  await flush()
  state.applicationId = 7
  state.chatId = 'oc_target'
  await flush()
  state.reviewedTarget = true
  state.clearConfirmed = true
}
beforeEach(() => {
  gets = []
  posts = []
  vi.clearAllMocks()
  api.get.mockImplementation((path, options) => deferred(gets, path, null, options))
  api.post.mockImplementation((path, body, options) => deferred(posts, path, body, options))
})
afterEach(() => {
  app?.unmount()
  app = null
  vi.useRealTimers()
})
it('never probes a write and makes no request for an ordinary user', () => {
  mount({ authorized: false })
  expect(gets).toHaveLength(0)
  expect(posts).toHaveLength(0)
})
it('shows default-off capability without loading applications or writing', async () => {
  mount()
  gets[0].resolve({ data: cap({ enabled: false }) })
  await flush()
  expect(gets).toHaveLength(1)
  expect(posts).toHaveLength(0)
  expect(state.enabled).toBe(false)
  expect(state.capability.enabled).toBe(false)
})
it('rejects a capability from a different source identity', async () => {
  mount()
  gets[0].resolve({ data: cap({ source_uuid: 'foreign' }) })
  await flush()
  expect(state.enabled).toBe(false)
  expect(posts).toHaveLength(0)
})
it('does not let an old bot response populate the new bot', async () => {
  mount()
  const old = gets[0]
  props.botId = 5
  props.botUuid = 'next-uuid'
  await flush()
  old.resolve({ data: cap({ latest_operation: status() }) })
  await flush()
  expect(state.operation).toBe(null)
  expect(old.options.signal.aborted).toBe(true)
  gets.at(-1).resolve({ data: cap({ enabled: false, source_uuid: 'next-uuid' }) })
  await flush()
  expect(state.operation).toBe(null)
})
it('requires target review and retains only application labels', async () => {
  mount()
  await ready()
  expect(state.canPlan).toBe(true)
  expect(state.applications).toEqual([{ id: 7, name: 'Target app' }])
  state.reviewedTarget = false
  await state.mutate('plan')
  expect(posts).toHaveLength(0)
})
it('creates exactly one explicit plan and drops extra response details', async () => {
  mount()
  await ready()
  const promise = state.mutate('plan')
  expect(posts).toHaveLength(1)
  expect(posts[0].path).toBe('/bots/4/migration-plan')
  const op = posts[0].body.operation_id
  posts[0].resolve({
    data: status({ operation_id: op, purpose: 'NEVER_RENDER', credential_ref: 'NEVER_RENDER' }),
  })
  await promise
  expect(state.operation.operation_id).toBe(op)
  expect(JSON.stringify(state.operation)).not.toContain('NEVER_RENDER')
  expect(posts).toHaveLength(1)
})
it('an ambiguous plan is inspected by GET rather than replayed', async () => {
  mount()
  await ready()
  const promise = state.mutate('plan')
  const op = posts[0].body.operation_id
  posts[0].reject(new Error('timeout'))
  await promise
  expect(state.pendingId).toBe(op)
  expect(state.canPlan).toBe(false)
  const checking = state.inspect()
  expect(gets.at(-1).path).toBe(`/bot-migrations/${op}`)
  gets.at(-1).resolve({ data: status({ operation_id: op }) })
  await checking
  expect(posts).toHaveLength(1)
})
it('clears target review and ignores a late plan when target changes', async () => {
  mount()
  await ready()
  const promise = state.mutate('plan')
  const request = posts[0]
  state.chatId = 'oc_another'
  await flush()
  request.resolve({ data: status({ operation_id: request.body.operation_id }) })
  await promise
  expect(request.options.signal.aborted).toBe(true)
  expect(state.operation).toBe(null)
  expect(state.reviewedTarget).toBe(false)
})
it('loads an existing operation without POST and fences an older confirmation', async () => {
  mount()
  gets[0].resolve({
    data: cap({ latest_operation: status({ next_action: 'resume', can_resume: true }) }),
  })
  await flush()
  let approve
  ElMessageBox.confirm.mockImplementation(
    () =>
      new Promise((resolve) => {
        approve = resolve
      })
  )
  const confirmed = state.confirmAction('resume')
  props.authScope = 'admin-b'
  await flush()
  approve()
  await confirmed
  expect(posts).toHaveLength(0)
  expect(state.operation).toBe(null)
})
it('uses exact phase on a user-requested resume and rejects mixed target status', async () => {
  mount()
  gets[0].resolve({
    data: cap({ latest_operation: status({ next_action: 'resume', can_resume: true }) }),
  })
  await flush()
  const transition = state.mutate('resume')
  expect(posts[0].body).toEqual({ expected_phase: 'planned' })
  posts[0].resolve({ data: status({ target_digest: 'b'.repeat(64) }) })
  await transition
  expect(state.operation.target_digest).toBe(digest)
  expect(state.error).toBe('migrationManagement.unconfirmed')
  expect(state.needsRefresh).toBe(true)
  await state.mutate('resume')
  expect(posts).toHaveLength(1)
})
it('never allows abort after identity switch and accepts its target revision for recovery', async () => {
  mount({ identityRevision: 3 })
  gets[0].resolve({
    data: cap({
      identity_revision: 3,
      supported_source: false,
      latest_operation: status({
        phase: 'switched',
        next_action: 'resume',
        reset_applied: true,
        can_abort: false,
      }),
    }),
  })
  await flush()
  expect(state.operation.phase).toBe('switched')
  await state.mutate('abort')
  expect(posts).toHaveLength(0)
})
it('invalidates old results after an authorization failure', async () => {
  mount()
  gets[0].resolve({ data: cap({ latest_operation: status() }) })
  await flush()
  const checking = state.inspect()
  gets.at(-1).reject({ response: { status: 403 } })
  await checking
  expect(state.operation).toBe(null)
  expect(state.pendingId).toBe('')
  expect(state.enabled).toBe(false)
  expect(state.busy).toBe(false)
})
it('rejects mixed scopes, future revisions and replay support in status', () => {
  for (const data of [
    status({ bot_id: 5 }),
    status({ source_revision: 9, target_revision: 10 }),
    status({ commands_replayed: true }),
    status({ reset_applied: 'invalid' }),
  ])
    expect(() => migrationStatus(data, 4, 2)).toThrow()
})

it('renders the actual restrained wide dialog with brief guidance and no raw payload dump', async () => {
  const view = createSSRApp(BotMigrationManagement, {
    botId: 4,
    botUuid: 'fixture',
    authorized: false,
  })
  view.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  for (const name of ['ElDialog', 'ElButton']) view.component(name, Container)
  view.config.warnHandler = () => {}
  const html = await renderToString(view)
  expect(html).toContain('width="980px"')
  expect(html).toContain(en.migrationManagement.intro)
  expect(html).not.toContain('app_secret')
  expect(posts).toHaveLength(0)
})

it('treats an absent management capability as unavailable without probing a mutation', async () => {
  mount()
  gets[0].reject({ response: { status: 404, data: { detail: 'NEVER_RENDER' } } })
  await flush()
  expect(state.enabled).toBe(false)
  expect(state.error).toBe('migrationManagement.unavailable')
  expect(posts).toHaveLength(0)
})
it('discards a late transition when the view is unmounted', async () => {
  mount()
  gets[0].resolve({ data: cap({ latest_operation: status() }) })
  await flush()
  const pending = state.mutate('resume')
  const request = posts[0]
  app.unmount()
  app = null
  request.resolve({ data: status({ phase: 'switched', reset_applied: true, can_abort: false }) })
  await pending
  expect(request.options.signal.aborted).toBe(true)
  expect(state.operation.phase).toBe('planned')
})
it('requires destructive acknowledgement and submits the exact reviewed reset digest', async () => {
  mount()
  await ready()
  state.clearConfirmed = false
  await state.mutate('plan')
  expect(posts).toHaveLength(0)
  state.clearConfirmed = true
  const pending = state.mutate('plan')
  expect(posts[0].body).toMatchObject({
    clear_live_state: true,
    reviewed_reset_digest: digest,
    target_application_id: 7,
    target_chat_id: 'oc_target',
  })
  posts[0].resolve({ data: status({ operation_id: posts[0].body.operation_id }) })
  await pending
  expect(state.operation.reset_counts).toEqual(resetCounts)
})
it('refuses unknown or negative clear counts instead of treating them as zero', async () => {
  mount()
  gets[0].resolve({
    data: cap({ reset_preview: { digest, counts: { ...resetCounts, holders: null } } }),
  })
  await flush()
  expect(state.enabled).toBe(false)
  expect(posts).toHaveLength(0)
  expect(() =>
    migrationStatus(status({ reset_counts: { ...resetCounts, offers: -1 } }), 4, 2)
  ).toThrow()
})
it('rejects a plan whose clear counts differ from the approved preview', async () => {
  mount()
  await ready()
  const pending = state.mutate('plan')
  posts[0].resolve({
    data: status({
      operation_id: posts[0].body.operation_id,
      reset_counts: { ...resetCounts, holders: 3 },
    }),
  })
  await pending
  expect(state.operation).toBe(null)
  expect(state.needsRefresh).toBe(true)
})
it('a changed source requires abort and a new review, never a force update', async () => {
  mount()
  gets[0].resolve({ data: cap({ latest_operation: status() }) })
  await flush()
  const pending = state.mutate('resume')
  posts[0].reject({
    response: { status: 409, data: { detail: { code: 'reset_preview_changed' } } },
  })
  await pending
  expect(state.stalePlan).toBe(true)
  const read = state.inspect()
  gets.at(-1).resolve({ data: status() })
  await read
  await state.mutate('resume')
  expect(posts).toHaveLength(1)
  const abort = state.mutate('abort')
  expect(posts[1].body).toEqual({ expected_phase: 'planned' })
  posts[1].resolve({
    data: status({ phase: 'aborted', next_action: 'aborted', can_abort: false, can_resume: false }),
  })
  await abort
  const fresh = state.load(true)
  gets.at(-1).resolve({
    data: cap({
      latest_operation: status({
        phase: 'aborted',
        next_action: 'aborted',
        can_abort: false,
        can_resume: false,
      }),
      reset_preview: { digest: 'b'.repeat(64), counts: { ...resetCounts, holders: 0 } },
    }),
  })
  await flush()
  gets.at(-1).resolve({ data: [{ id: 7, name: 'Target app' }] })
  await fresh
  expect(state.operation).toBe(null)
  expect(state.clearConfirmed).toBe(false)
  expect(state.capability.reset_preview.counts.holders).toBe(0)
})

it('uses reset evidence rather than obsolete active-state carry metadata', () => {
  const data = status()
  delete data.active_state_migration_supported
  expect(migrationStatus(data, 4, 2).reset_counts).toEqual(resetCounts)
})
