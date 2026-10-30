/* eslint-disable vue/one-component-per-file -- Local Element Plus slot stubs exercise the rendered panel. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, createSSRApp, defineComponent, h, nextTick, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createI18n } from 'vue-i18n'
import en from '../i18n/en'
import zhCN from '../i18n/zh-CN'
import BotMigrationPreflightResult from './BotMigrationPreflightResult.vue'
import BotMigrationPreflight from './BotMigrationPreflight.vue'
import api from '../utils/api'

vi.mock('../utils/api', () => ({ default: { get: vi.fn(), post: vi.fn() } }))
const renderer = createRenderer({
  createElement: (tag) => ({ tag, children: [], props: {} }),
  createText: (text) => ({ text }),
  createComment: () => ({ text: '' }),
  insert(child, parent, anchor) {
    child.parent = parent
    const index = parent.children.indexOf(anchor)
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child)
  },
  remove(child) {
    const index = child.parent?.children.indexOf(child)
    if (index >= 0) child.parent.children.splice(index, 1)
  },
  setText: (node, text) => (node.text = text),
  setElementText(node, text) {
    node.text = text
    node.children = []
  },
  parentNode: (node) => node.parent,
  nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1],
  patchProp: (node, key, _previous, value) => (node.props[key] = value),
})
const Container = defineComponent({
  setup:
    (_, { slots }) =>
    () =>
      h('div', [slots.label?.(), slots.default?.(), slots.footer?.()]),
})
const Button = defineComponent({
  props: { disabled: Boolean },
  setup:
    (props, { slots }) =>
    () =>
      h('button', { disabled: props.disabled }, slots.default?.()),
})
const flush = async () => {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}
const text = (node) => [node.text || '', ...(node.children || []).map(text)].join(' ')
let app, state, root, gets, posts
function deferred(list, path, options, body) {
  return new Promise((resolve, reject) => list.push({ path, options, body, resolve, reject }))
}
function mount(props = {}) {
  root = { children: [] }
  app = renderer.createApp(BotMigrationPreflight, { botId: 1, authorized: true, ...props })
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  app.config.globalProperties.$t = (key) => key
  for (const name of [
    'ElDialog',
    'ElForm',
    'ElFormItem',
    'ElSelect',
    'ElOption',
    'ElInput',
    'ElTooltip',
    'ElIcon',
  ])
    app.component(name, Container)
  app.component('ElButton', Button)
  app.mount(root)
  state = app._instance.setupState
}
async function selectTarget(chat = 'oc_target') {
  gets.at(-1).resolve({ data: [{ id: 7, name: 'Target app', app_secret: 'MUST_NOT_RETAIN' }] })
  await flush()
  state.applicationId = 7
  state.chatId = chat
  await flush()
}
function response(bot = 1, chat = 'oc_target') {
  return {
    schema_version: 1,
    read_only: true,
    can_commit: false,
    source: { bot_id: bot, identity_revision: 0 },
    target: { application_id: 7, chat_id: chat },
    stored_obligations: { resources: 4, holders: 2, reservations: 1, offers: 0, non_idle: 2 },
    blockers: ['source_has_outstanding_allocations', 'cutover_not_implemented'],
    unverified: ['current_group_membership'],
  }
}
beforeEach(() => {
  vi.clearAllMocks()
  gets = []
  posts = []
  api.get.mockImplementation((path, options) => deferred(gets, path, options))
  api.post.mockImplementation((path, body, options) => deferred(posts, path, options, body))
})
afterEach(() => {
  app?.unmount()
  app = null
})

it('renders actual read-only counts and blockers without a cutover control or retained secrets', async () => {
  mount()
  await selectTarget()
  expect(JSON.stringify(state.applications)).not.toContain('MUST_NOT_RETAIN')
  const checking = state.check()
  expect(posts[0].path).toBe('/bots/1/migration-preflight')
  expect(posts[0].body).toEqual({ application_id: 7, chat_id: 'oc_target' })
  posts[0].resolve({ data: { ...response(), app_secret: 'MUST_NOT_RETAIN' } })
  await checking
  await flush()
  const presentation = createSSRApp(BotMigrationPreflightResult, { result: state.result })
  presentation.config.globalProperties.$t = (key) => key
  const html = await renderToString(presentation)
  expect(html).toContain('migration.cannotMigrate')
  expect(html).toContain('migration.blockers.source_has_outstanding_allocations')
  expect(html).toContain('migration.unverified.current_group_membership')
  expect(html).toMatch(/<dd[^>]*>2<\/dd>/)
  expect(html).not.toContain('<button')
  expect(state.result.stored_obligations.holders).toBe(2)
  expect(JSON.stringify(state.result)).not.toContain('MUST_NOT_RETAIN')
})

it('blocks calls without current authorization or a selected application and valid group', async () => {
  mount({ authorized: false })
  await state.check()
  expect(gets).toHaveLength(0)
  expect(posts).toHaveLength(0)
  app._instance.props.authorized = true
  await flush()
  await selectTarget('not-a-chat')
  await state.check()
  expect(posts).toHaveLength(0)
  state.chatId = 'oc_valid'
  state.applicationId = 999
  await state.check()
  expect(posts).toHaveLength(0)
})

it.each([401, 403])('clears prior data and disables checks on current API %s', async (status) => {
  mount()
  await selectTarget()
  const checking = state.check()
  posts[0].reject({ response: { status, data: { detail: 'DO_NOT_DISPLAY' } } })
  await checking
  await flush()
  expect(state.result).toBeNull()
  expect(state.applications).toEqual([])
  expect(state.denied).toBe(true)
  expect(state.canCheck).toBe(false)
  expect(text(root)).not.toContain('DO_NOT_DISPLAY')
  expect(state.error).toBe(status === 401 ? 'auth.sessionExpired' : 'common.permissionDenied')
})

it('discards late results and errors after target edits without poisoning the new check', async () => {
  mount()
  await selectTarget()
  const old = state.check()
  state.chatId = 'oc_new'
  expect(posts[0].options.signal.aborted).toBe(true)
  expect(state.result).toBeNull()
  const current = state.check()
  posts[0].reject({ response: { status: 403 } })
  await old
  expect(state.denied).toBe(false)
  expect(state.checking).toBe(true)
  posts[1].resolve({ data: response(1, 'oc_new') })
  await current
  expect(state.result.stored_obligations.resources).toBe(4)
})

it('isolates bot revisits and aborts pending work when permissions or revision change', async () => {
  mount()
  await selectTarget()
  const old = state.check()
  app._instance.props.botId = 2
  await flush()
  expect(posts[0].options.signal.aborted).toBe(true)
  app._instance.props.botId = 1
  await flush()
  posts[0].resolve({ data: response() })
  await old
  expect(state.result).toBeNull()
  await selectTarget()
  const next = state.check()
  app._instance.props.identityRevision = 1
  await flush()
  expect(posts[1].options.signal.aborted).toBe(true)
  posts[1].resolve({ data: response() })
  await next
  expect(state.result).toBeNull()
  app._instance.props.authorized = false
  await flush()
  expect(gets.at(-1).options.signal.aborted).toBe(true)
  expect(state.applications).toEqual([])
})

it('fails closed for another bot, target or future executable response', async () => {
  mount()
  await selectTarget()
  for (const data of [response(2), response(1, 'oc_other'), { ...response(), can_commit: true }]) {
    const checking = state.check()
    posts.at(-1).resolve({ data })
    await checking
    expect(state.result).toBeNull()
    expect(state.error).toBe('migration.invalidResponse')
  }
})

it('shows a current unavailable endpoint and discards late app lists after unmount', async () => {
  mount()
  await selectTarget()
  const checking = state.check()
  posts[0].reject({ response: { status: 404 } })
  await checking
  expect(state.error).toBe('migration.notAvailable')
  app._instance.props.botId = 2
  await flush()
  const pending = gets.at(-1)
  app.unmount()
  app = null
  expect(pending.options.signal.aborted).toBe(true)
  pending.resolve({ data: [{ id: 8, name: 'Late app' }] })
  await flush()
  expect(state.applications).toEqual([])
})

it('renders only a real preflight action and close control, with no migration submit button', async () => {
  const shell = createSSRApp(BotMigrationPreflight, { botId: 1, authorized: true })
  shell.config.warnHandler = () => {}
  shell.config.globalProperties.$t = (key) => key
  for (const name of [
    'ElDialog',
    'ElForm',
    'ElFormItem',
    'ElSelect',
    'ElOption',
    'ElInput',
    'ElTooltip',
    'ElIcon',
  ])
    shell.component(name, Container)
  shell.component('ElButton', Button)
  const html = await renderToString(shell)
  expect(html).toContain('migration.readOnly')
  expect(html).toContain('migration.check')
  expect(html).toContain('common.close')
  expect(html.match(/<button\b/g)).toHaveLength(2)
  expect(html).not.toContain('common.confirm')
  expect(html).not.toContain('type="submit"')
  expect(posts).toHaveLength(0)
})

it('compiles the actual migration translations without linked-message errors', () => {
  const errors = []
  for (const [locale, messages] of Object.entries({ en, 'zh-CN': zhCN })) {
    const translator = createI18n({ legacy: false, locale, messages: { [locale]: messages } })
    const visit = (value, path) => {
      for (const [key, entry] of Object.entries(value)) {
        if (typeof entry === 'string')
          expect(() => translator.global.t(`${path}.${key}`)).not.toThrow()
        else visit(entry, `${path}.${key}`)
      }
    }
    const log = vi.spyOn(console, 'error').mockImplementation((...args) => errors.push(args))
    try {
      visit(messages.migration, 'migration')
      if (locale === 'zh-CN') expect(translator.global.t('migration.chatHelp')).toContain('@机器人')
      expect(errors).toEqual([])
    } finally {
      log.mockRestore()
    }
  }
})
