/* eslint-disable vue/one-component-per-file -- minimal host stubs for actual SFC event bindings */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as Vue from 'vue'
import { createRenderer, defineComponent, h, nextTick, reactive, ssrContextKey } from 'vue'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { readFileSync } from 'node:fs'
import Page from './AgentTraces.vue'
import api from '../utils/api'
const state = vi.hoisted(() => ({ route: null, router: null, auth: null }))
vi.mock('vue-router', () => ({ useRoute: () => state.route, useRouter: () => state.router }))
vi.mock('../stores/auth', () => ({ useAuthStore: () => state.auth }))
vi.mock('../utils/demoMode', () => ({ LS_KEYS: { token: 'lockbot_token', user: 'lockbot_user' } }))
vi.mock('../utils/api', () => ({ default: { get: vi.fn(), post: vi.fn(), patch: vi.fn() } }))
const filename = new URL('./AgentTraces.vue', import.meta.url).pathname
const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
const bindings = compileScript(descriptor, { id: 'comments-interaction' }).bindings
const { code } = compileTemplate({
  source: descriptor.template.content,
  filename,
  id: 'comments-interaction',
  compilerOptions: { bindingMetadata: bindings },
})
Page.render = new Function(
  'Vue',
  code
    .replace(
      /import \{([^}]+)\} from "vue"/g,
      (_, names) => `const {${names.replaceAll(' as ', ': ')}} = Vue`
    )
    .replace('export function render', 'return function render')
)(Vue)
const renderer = createRenderer({
  createElement: (tag) => ({
    tag,
    children: [],
    props: {},
    ...(tag === 'iframe' ? { contentWindow: { postMessage: vi.fn() } } : {}),
  }),
  createText: (text) => ({ text }),
  createComment: (text) => ({ text }),
  insert(node, parent, anchor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
    node.parent = parent
    const i = parent.children.indexOf(anchor)
    parent.children.splice(i < 0 ? parent.children.length : i, 0, node)
  },
  remove(node) {
    const i = node?.parent?.children.indexOf(node) ?? -1
    if (i >= 0) node.parent.children.splice(i, 1)
  },
  setText(node, text) {
    node.text = text
  },
  setElementText(node, text) {
    node.text = text
    node.children = []
  },
  parentNode: (node) => node.parent,
  nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1],
  patchProp(node, key, _old, value) {
    node.props[key] = value
  },
})
function storage() {
  const values = new Map()
  return {
    values,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  }
}
const source = 'a'.repeat(64),
  runA = '1'.repeat(64),
  runB = '2'.repeat(64)
const context = (caseId = 'A', runId = runA, datasetId = source) => ({
  scope: 'run',
  case_id: caseId,
  case_name: 'Case ' + caseId,
  model: 'Model',
  run_index: 0,
  run_id: runId,
  dataset_id: datasetId,
  server_backed: true,
})
const comment = (target, id = 'comment') => ({
  ...target,
  id,
  raw: true,
  status: 'open',
  author: 'Reviewer',
  created_at: '2026-11-18T00:00:00Z',
  anchor: 'run',
  body: 'comment ' + target.case_id,
})
const flush = async () => {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}
let local, tabs
function useTab(tab) {
  vi.stubGlobal('sessionStorage', tab.session)
  vi.stubGlobal('window', {
    addEventListener: (name, handler) => tab.listeners.set(name, handler),
    removeEventListener: (name) => tab.listeners.delete(name),
  })
}
function all(tab, predicate, node = tab.root) {
  return [
    ...(predicate(node) ? [node] : []),
    ...(node.children || []).flatMap((child) => all(tab, predicate, child)),
  ]
}
function button(tab, label) {
  return all(
    tab,
    (node) => node.tag === 'button' && all(tab, (child) => child.text?.includes(label), node).length
  )[0]
}
function frame(tab) {
  return all(tab, (node) => node.tag === 'iframe')[0]
}
async function mount(session = storage()) {
  const tab = { session, listeners: new Map(), root: { children: [] } }
  useTab(tab)
  const app = renderer.createApp({ render: () => h(Page) })
  tab.app = app
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  const box = defineComponent({
    setup:
      (_, { slots }) =>
      () =>
        h('section', {}, slots.default?.()),
  })
  for (const name of [
    'ElDrawer',
    'ElSelect',
    'ElOption',
    'ElDivider',
    'ElEmpty',
    'ElCollapse',
    'ElCollapseItem',
    'ElCheckbox',
    'ElTag',
  ])
    app.component(name, box)
  app.component(
    'ElButton',
    defineComponent({
      setup:
        (_, { slots }) =>
        () =>
          h('button', {}, slots.default?.()),
    })
  )
  app.component(
    'ElAlert',
    defineComponent({
      props: { title: { type: String, default: '' } },
      setup: (props) => () => h('output', {}, props.title),
    })
  )
  app.component(
    'ElInput',
    defineComponent({
      props: { modelValue: { type: String, default: '' } },
      emits: ['update:modelValue'],
      setup:
        (props, { emit }) =>
        () =>
          h('textarea', {
            value: props.modelValue,
            onInput: (event) => emit('update:modelValue', event.target.value),
          }),
    })
  )
  app.mount(tab.root)
  tab.view = app._instance.subTree.component.setupState
  tabs.push(tab)
  await flush()
  return tab
}
async function message(tab, type, target, extra = {}) {
  useTab(tab)
  tab.listeners.get('message')({
    origin: location.origin,
    source: tab.view.viewer.contentWindow,
    data: {
      type: 'lockbot-trace-' + type,
      context: target,
      route: {
        case: target.case_id,
        run: String(target.run_index),
        ...(state.route.query.dataset_id ? { dataset_id: target.dataset_id } : {}),
        ...(state.route.query.review_run ? { review_run: state.route.query.review_run } : {}),
      },
      ...extra,
    },
  })
  await flush()
}
async function open(tab, target = context()) {
  await message(tab, 'comment', target, {
    anchor: 'message:0:0:text',
    quote: 'original quote',
    field_label: '回答',
  })
}
async function type(tab, body) {
  useTab(tab)
  all(tab, (node) => node.tag === 'textarea')[0].props.onInput({ target: { value: body } })
  await flush()
}
beforeEach(() => {
  tabs = []
  local = storage()
  local.setItem('lockbot_token', 'test-token')
  local.setItem('lockbot_user', JSON.stringify({ id: 1, username: 'one' }))
  vi.stubGlobal('localStorage', local)
  vi.stubGlobal('location', { origin: 'http://localhost' })
  vi.stubGlobal('document', { documentElement: { classList: { contains: () => false } } })
  vi.stubGlobal(
    'MutationObserver',
    class {
      observe() {}
      disconnect() {}
    }
  )
  state.auth = reactive({ token: 'test-token', user: { id: 1, username: 'one' } })
  state.route = reactive({ query: {} })
  state.router = {
    push: vi.fn(async (value) => {
      state.route.query = value.query
    }),
    replace: vi.fn(async (value) => {
      state.route.query = value.query
    }),
  }
  api.get.mockReset().mockImplementation(async (_url, options) => ({
    data: {
      comments: [
        comment(context(options.params.case_id, options.params.case_id === 'A' ? runA : runB)),
      ],
    },
  }))
  api.post.mockReset().mockResolvedValue({ data: {} })
  api.patch.mockReset().mockResolvedValue({ data: {} })
})
afterEach(() => {
  for (const tab of tabs) {
    useTab(tab)
    tab.app.unmount()
  }
  vi.unstubAllGlobals()
})
it('isolates tabs, saves only this draft, and restores the other after reload', async () => {
  const a = await mount()
  await open(a)
  await type(a, 'draft A')
  const b = await mount()
  await open(b, context('B', runB))
  await type(b, 'draft B')
  useTab(a)
  button(a, '保存评论').props.onClick()
  await flush()
  expect(api.post.mock.calls[0][1]).toMatchObject({ case_id: 'A', body: 'draft A' })
  useTab(b)
  b.app.unmount()
  tabs.splice(tabs.indexOf(b), 1)
  const restored = await mount(b.session)
  expect(restored.view.draft).toBe('draft B')
  expect(restored.view.draftContext.case_id).toBe('B')
  expect(
    [...a.session.values.keys()].filter((key) => key.startsWith('lockbot_trace_review_draft:'))
  ).toEqual([])
  expect(local.values.has('lockbot_trace_review_draft')).toBe(false)
})
it.each([
  ['ducc-luna · gpt-6-luna', 'Luna'],
  ['ducc-glm · GLM-5.3', 'GLM 5.3'],
  ['Unknown client · exact-model-id', 'Unknown client · exact-model-id'],
])('formats comment display without changing %s identity or its saved target', async (model, label) => {
  const tab = await mount()
  const target = { ...context(), model }
  const before = JSON.stringify(target)
  await open(tab, target)
  expect(all(tab, (node) => node.props?.class === 'review-context').some((node) =>
    node.text === 'Case A · ' + label)).toBe(true)
  expect(tab.view.draftContext.model).toBe(model)
  await type(tab, 'Review this run')
  const stored = [...tab.session.values].find(([key]) => key.startsWith('lockbot_trace_review_draft:'))
  expect(JSON.parse(stored[1]).context).toMatchObject({ model, run_id: runA, dataset_id: source })
  button(tab, '保存评论').props.onClick()
  await flush()
  expect(api.post.mock.calls[0][1]).toMatchObject({
    case_id: 'A', run_index: 0, run_id: runA, dataset_id: source, body: 'Review this run',
  })
  expect(JSON.stringify(target)).toBe(before)
})
it('updates an empty composer on Back, clearing its previous quotation before saving', async () => {
  const tab = await mount()
  await open(tab)
  await message(tab, 'route', context('B', runB))
  expect(tab.view.draftContext).toMatchObject({ case_id: 'B', quote: '', anchor: 'run' })
  await type(tab, 'belongs to B')
  button(tab, '保存评论').props.onClick()
  await flush()
  expect(api.post.mock.calls[0][1]).toMatchObject({
    case_id: 'B',
    run_id: runB,
    quote: '',
    body: 'belongs to B',
  })
})
it('pins the complete drawer and list to a nonempty draft across viewer navigation', async () => {
  const tab = await mount()
  await open(tab)
  await type(tab, 'keep A')
  const reads = api.get.mock.calls.length
  await message(tab, 'route', context('B', runB))
  expect(api.get).toHaveBeenCalledTimes(reads)
  expect(tab.view.current.case_id).toBe('B')
  expect(tab.view.draftContext.case_id).toBe('A')
  expect(tab.view.currentComments.map((row) => row.case_id)).toEqual(['A'])
  button(tab, '保存评论').props.onClick()
  await flush()
  expect(api.post.mock.calls[0][1].case_id).toBe('A')
  expect(tab.view.currentComments.map((row) => row.case_id)).toEqual(['A'])
  button(tab, '清空草稿').props.onClick()
  await flush()
  expect(tab.view.draftContext.case_id).toBe('B')
})
it('separates users and discards delayed comments from an old authentication generation', async () => {
  const tab = await mount()
  await open(tab)
  await type(tab, 'private one')
  let complete
  api.get.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        complete = resolve
      })
  )
  tab.view.loadComments(context())
  await flush()
  local.setItem('lockbot_token', 'other-token')
  local.setItem('lockbot_user', JSON.stringify({ id: 2, username: 'two' }))
  state.auth.token = 'other-token'
  state.auth.user = { id: 2, username: 'two' }
  await flush()
  complete({ data: { comments: [comment(context(), 'private')] } })
  await flush()
  expect(tab.view.comments).toEqual([])
  expect(tab.view.draft).toBe('')
  expect(tab.view.reviewOpen).toBe(false)
  local.setItem('lockbot_token', 'test-token')
  local.setItem('lockbot_user', JSON.stringify({ id: 1, username: 'one' }))
  state.auth.token = 'test-token'
  state.auth.user = { id: 1, username: 'one' }
  await flush()
  expect(tab.view.draft).toBe('private one')
})
it.each(['trace_changed', 'comment_store_full'])(
  'retains the body and gives actionable %s feedback',
  async (code) => {
    const tab = await mount()
    await open(tab)
    await type(tab, 'never lose this')
    api.post.mockRejectedValueOnce({ response: { status: 409, data: { code } } })
    button(tab, '保存评论').props.onClick()
    await flush()
    expect(tab.view.draft).toBe('never lose this')
    expect(tab.view.saveError).toContain(code === 'trace_changed' ? '重新选择原文' : '存储已满')
    expect(tab.view.saveError).not.toContain('刷新')
    if (code === 'trace_changed') {
      button(tab, '重新选择原文').props.onClick()
      await flush()
      await open(tab, context('B', runB))
      expect(tab.view.draftContext.case_id).toBe('B')
      expect(tab.view.draft).toBe('never lose this')
    }
  }
)
it('loads cross-source history and locates only its exact original coordinates', async () => {
  const old = {
    ...comment(context('A', '3'.repeat(64), 'b'.repeat(64)), 'old'),
    source_available: true,
    run_index: 7,
  }
  api.get.mockResolvedValue({ data: { comments: [comment(context()), old] } })
  const tab = await mount()
  await open(tab)
  expect(api.get.mock.calls[0][1].params.include_history).toBe(true)
  expect(tab.view.historicalComments).toEqual([old])
  button(tab, '查看当时记录').props.onClick()
  await flush()
  expect(state.route.query).toEqual({
    case: 'A',
    run: '7',
    dataset_id: 'b'.repeat(64),
    review_run: old.run_id,
  })
  expect(frame(tab).props.src).toContain('dataset_id=' + 'b'.repeat(64))
  const calls = frame(tab).contentWindow.postMessage
  calls.mockClear()
  await message(tab, 'route', { ...context(), run_index: 7 })
  expect(calls.mock.calls.some(([value]) => value.type === 'lockbot-trace-locate')).toBe(false)
  // Restore the intended history URL after this deliberately mismatched source notification.
  tab.view.locate(old)
  await flush()
  await message(tab, 'route', { ...context('A', old.run_id, old.dataset_id), run_index: 7 })
  expect(
    calls.mock.calls.some(
      ([value]) => value.type === 'lockbot-trace-locate' && value.run_id === old.run_id
    )
  ).toBe(true)
  button(tab, '返回当前记录').props.onClick()
  await flush()
  expect(state.route.query.dataset_id).toBeUndefined()
  expect(frame(tab).props.src).not.toContain('dataset_id=')
})

it('preserves old unowned drafts and supports HTTP browsers without randomUUID', async () => {
  vi.stubGlobal('crypto', {})
  local.setItem('lockbot_trace_review_draft', 'legacy draft without an author')
  const tab = await mount()
  expect(tab.view.draft).toBe('')
  await open(tab)
  await type(tab, 'new scoped draft')
  expect(
    [...tab.session.values.keys()].some((key) => key.startsWith('lockbot_trace_review_draft:1:'))
  ).toBe(true)
  button(tab, '清空草稿').props.onClick()
  await flush()
  expect(local.getItem('lockbot_trace_review_draft')).toBe('legacy draft without an author')
})

it.each([false, true])(
  'quarantines token-first account transitions until /auth/me replaces the user (same account: %s)',
  async (sameAccount) => {
    const tab = await mount()
    await open(tab)
    await type(tab, 'former draft')
    const reads = api.get.mock.calls.length
    // This is the actual switchAccount order: token first, then awaited /auth/me.
    local.setItem('lockbot_token', 'new-token')
    state.auth.token = 'new-token'
    await flush()
    expect(tab.view.reviewOpen).toBe(false)
    expect(tab.view.draft).toBe('')
    expect(tab.view.comments).toEqual([])
    expect(api.get).toHaveBeenCalledTimes(reads)
    const user = sameAccount ? { id: 1, username: 'one' } : { id: 2, username: 'two' }
    local.setItem('lockbot_user', JSON.stringify(user))
    state.auth.user = user
    await flush()
    expect(tab.view.draft).toBe(sameAccount ? 'former draft' : '')
    expect(api.get).toHaveBeenCalledTimes(reads + (sameAccount ? 1 : 0))
  }
)

it('opens a verified legacy comment through an explicit legacy route without borrowing the current board', async () => {
  const old = {
    ...comment({ ...context('A', '3'.repeat(64)), dataset_id: null }, 'legacy'),
    source_available: true,
    run_index: 4,
  }
  api.get.mockResolvedValue({ data: { comments: [comment(context()), old] } })
  const tab = await mount()
  await open(tab)
  button(tab, '查看当时记录').props.onClick()
  await flush()
  expect(state.route.query).toEqual({ case: 'A', run: '4', legacy: '1', review_run: old.run_id })
  expect(frame(tab).props.src).toContain('legacy=1')
  const calls = frame(tab).contentWindow.postMessage
  calls.mockClear()
  await message(tab, 'route', { ...context(), run_index: 4 })
  expect(calls.mock.calls.some(([value]) => value.type === 'lockbot-trace-locate')).toBe(false)
  await message(
    tab,
    'route',
    { ...context('A', old.run_id), dataset_id: null, run_index: 4 },
    { route: { case: 'A', run: '4', legacy: '1', review_run: old.run_id } }
  )
  expect(
    calls.mock.calls.some(
      ([value]) => value.type === 'lockbot-trace-locate' && value.run_id === old.run_id
    )
  ).toBe(true)
  button(tab, '返回当前记录').props.onClick()
  await flush()
  expect(state.route.query.legacy).toBeUndefined()
  expect(frame(tab).props.src).not.toContain('legacy=1')
})

it('lets explicit historical navigation clear the locator and reloads on a new historical identity', async () => {
  const old = {
    ...comment(context('A', '3'.repeat(64), 'b'.repeat(64)), 'old'),
    source_available: true,
    run_index: 7,
  }
  const tab = await mount()
  tab.view.locate(old)
  await flush()
  const first = frame(tab)
  const revision = tab.view.viewerRevision
  await message(tab, 'route', context('B', runB, old.dataset_id), {
    navigation: 'push',
    route: { case: 'B', run: '0', dataset_id: old.dataset_id },
  })
  expect(tab.view.current.case_id).toBe('B')
  expect(state.route.query.case).toBe('B')
  expect(state.route.query.review_run).toBeUndefined()
  expect(frame(tab)).not.toBe(first)
  expect(tab.view.viewerRevision).toBeGreaterThan(revision)
  tab.view.locate(old)
  await flush()
  const second = frame(tab)
  tab.view.locate({ ...old, run_id: '4'.repeat(64), run_index: 8 })
  await flush()
  expect(frame(tab)).not.toBe(second)
  expect(frame(tab).props.src).toContain('review_run=' + '4'.repeat(64))
})
