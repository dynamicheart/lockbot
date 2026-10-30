/* eslint-disable vue/one-component-per-file -- minimal host stubs for the real SFC template */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRenderer, defineComponent, h, nextTick, reactive, ssrContextKey } from 'vue'
import Panel from './ServiceTrafficPanel.vue'
import api from '../utils/api'
import * as Vue from 'vue'
import { readFileSync } from 'node:fs'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'

// Vitest loads SFCs in SSR mode; compile the production DOM template as well so
// these tests exercise actual button/event/tooltip bindings, not just setup helpers.
const filename = new URL('./ServiceTrafficPanel.vue', import.meta.url).pathname
const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
const bindings = compileScript(descriptor, { id: 'traffic-interaction' }).bindings
const { code } = compileTemplate({
  source: descriptor.template.content,
  filename,
  id: 'traffic-interaction',
  compilerOptions: { bindingMetadata: bindings },
})
Panel.render = new Function(
  'Vue',
  code
    .replace(
      /import \{([^}]+)\} from "vue"/g,
      (_line, names) => `const {${names.replaceAll(' as ', ': ')}} = Vue`
    )
    .replace('export function render', 'return function render')
)(Vue)

vi.mock('../utils/api', () => ({ default: { get: vi.fn() } }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key, locale: { value: 'en' } }) }))
const renderer = createRenderer({
  createElement: (tag) => ({ tag, children: [], props: {} }),
  createText: (text) => ({ text }),
  createComment: (text) => ({ text }),
  insert: (node, parent, anchor) => {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
    node.parent = parent
    const index = parent.children.indexOf(anchor)
    parent.children.splice(index < 0 ? parent.children.length : index, 0, node)
  },
  remove: (node) => {
    const index = node?.parent?.children.indexOf(node) ?? -1
    if (index >= 0) node.parent.children.splice(index, 1)
  },
  setText: (node, text) => {
    node.text = text
  },
  setElementText: (node, text) => {
    node.text = text
    node.children = []
  },
  parentNode: (node) => node.parent,
  nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1],
  patchProp: (node, key, _old, value) => {
    node.props[key] = value
  },
})
const Tooltip = defineComponent({
  props: { content: { type: String, default: '' }, visible: Boolean },
  setup:
    (props, { slots }) =>
    () =>
      h('section', {}, [slots.default?.(), props.visible ? h('output', {}, props.content) : null]),
})
const Card = defineComponent({
  setup:
    (_props, { slots }) =>
    () =>
      h('section', {}, [slots.header?.(), slots.default?.()]),
})
const at = ['2026-10-30T12:00:00Z', '2026-10-30T12:01:00Z', '2026-10-30T12:02:00Z']
const fixture = () => ({
  started_at: at[0],
  captured_at: at[2],
  totals: { requests: 8, p95_ms: 3, error_rate: 0, request_bytes: 0, response_bytes: 0 },
  last_minute: { requests: 1 },
  history: at.map((time, i) => ({ at: time, requests: [0, 7, 1][i], server_errors: [0, 2, 0][i] })),
  business_operations: {
    history: [
      { at: at[0], observed: true, sources: { chat: 0, agent: 0, operator: 0 } },
      { at: at[1], observed: true, sources: { chat: 2, agent: 3, operator: 1 } },
    ],
    rows: [],
  },
  sources: [],
})
let app, root, props, pending, listeners
const flush = async () => {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}
function find(predicate, node = root) {
  return [
    ...(predicate(node) ? [node] : []),
    ...(node.children || []).flatMap((child) => find(predicate, child)),
  ]
}
const buttons = () =>
  find((node) => node.tag === 'button' && node.props.class?.includes('traffic-minute'))
const outputs = () => find((node) => node.tag === 'output').map((node) => node.text)
async function mount(initial = { botId: null }) {
  props = reactive(initial)
  root = { children: [] }
  app = renderer.createApp({ setup: () => () => h(Panel, props) })
  app.provide(ssrContextKey, { modules: new Set() })
  app.config.warnHandler = () => {}
  app.config.globalProperties.$t = (key) => key
  app.directive('loading', {})
  app.component('ElCard', Card)
  app.component('ElTable', defineComponent({ setup: () => () => h('div') }))
  app.component('ElTooltip', Tooltip)
  app.mount(root)
  pending[0].resolve({ data: fixture() })
  await flush()
  return app._instance.subTree.component.setupState
}
beforeEach(() => {
  vi.useFakeTimers()
  pending = []
  listeners = new Map()
  api.get
    .mockReset()
    .mockImplementation(
      (path, options) => new Promise((resolve) => pending.push({ path, options, resolve }))
    )
  vi.stubGlobal('document', {
    hidden: false,
    addEventListener: (name, handler) => listeners.set(name, handler),
    removeEventListener: (name) => listeners.delete(name),
  })
})
afterEach(() => {
  app?.unmount()
  app = null
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('renders exact sparse/error/source counts for one minute and aligns both charts without requests', async () => {
  const view = await mount()
  expect(buttons()).toHaveLength(6)
  const count = api.get.mock.calls.length
  buttons()[1].props.onPointerenter({ pointerType: 'mouse' })
  await nextTick()
  expect(view.activeMinute).toBe(at[1])
  expect(buttons().filter((node) => node.props.class.includes('is-active'))).toHaveLength(2)
  expect(outputs()).toEqual([view.httpTooltip(fixture().history[1])])
  expect(outputs()[0]).toContain(new Date(at[1]).toLocaleString('en'))
  expect(outputs()[0]).toContain('serviceTraffic.requests: 7')
  expect(outputs()[0]).toContain('serviceTraffic.serverErrors: 2')
  buttons()[4].props.onPointerenter({ pointerType: 'mouse' })
  await nextTick()
  expect(outputs()[0]).toContain('serviceTraffic.businessSources.chat: 2')
  expect(outputs()[0]).toContain('serviceTraffic.businessSources.agent: 3')
  expect(outputs()[0]).toContain('serviceTraffic.businessSources.operator: 1')
  expect(outputs()[0]).toContain('serviceTraffic.minuteTotal: 6')
  expect(api.get).toHaveBeenCalledTimes(count)
  buttons()[4].props.onPointerleave({ pointerType: 'mouse' })
  await nextTick()
  expect(outputs()).toEqual([])
  expect(view.activeMinute).toBeNull()
})

it('distinguishes observed zero from missing operation evidence, including chart edges', async () => {
  await mount()
  buttons()[3].props.onFocus()
  await nextTick()
  expect(outputs()[0]).toContain('serviceTraffic.minuteTotal: 0')
  buttons()[5].props.onFocus()
  await nextTick()
  expect(outputs()[0]).toContain('serviceTraffic.operationHistoryUnavailable')
  expect(outputs()[0]).not.toContain('serviceTraffic.minuteTotal')
  buttons()[5].props.onBlur()
  await nextTick()
  expect(outputs()).toEqual([])
})

it('supports keyboard movement/Escape and touch selection/dismissal without request or layout data changes', async () => {
  const view = await mount()
  const nodes = buttons().slice(0, 3)
  nodes.forEach((node) => {
    node.focus = () => node.props.onFocus()
  })
  const currentTarget = { closest: () => ({ querySelectorAll: () => nodes }) }
  // Use the actual host node identity for index lookup.
  nodes[0].closest = currentTarget.closest
  const preventDefault = vi.fn()
  nodes[0].props.onKeydown({ key: 'End', currentTarget: nodes[0], preventDefault })
  await nextTick()
  expect(view.activeMinute).toBe(at[2])
  expect(nodes[2].props.tabindex).toBe(0)
  nodes[2].props.onKeydown({ key: 'Escape', preventDefault })
  await nextTick()
  expect(outputs()).toEqual([])
  const touch = { pointerType: 'touch', preventDefault }
  buttons()[1].props.onPointerdown(touch)
  await nextTick()
  expect(view.activeMinute).toBe(at[1])
  buttons()[1].props.onPointerleave({ pointerType: 'touch' })
  expect(view.activeMinute).toBe(at[1])
  listeners.get('pointerdown')({ target: { closest: () => null } })
  await nextTick()
  expect(view.activeMinute).toBeNull()
  expect(api.get).toHaveBeenCalledTimes(1)
  expect(preventDefault).toHaveBeenCalled()
})

it('clears on refresh, response replacement, grouping/filter/bot changes and unmount', async () => {
  const view = await mount()
  const select = () => view.selectMinute(at[1], 'http')
  select()
  view.grouping = 'routes'
  await nextTick()
  expect(view.activeMinute).toBeNull()
  select()
  view.refresh()
  expect(view.activeMinute).toBeNull()
  select()
  pending.at(-1).resolve({ data: fixture() })
  await flush()
  expect(view.activeMinute).toBeNull()
  select()
  view.selectedBot = 2
  expect(view.activeMinute).toBeNull()
  select()
  props.botId = 3
  await nextTick()
  expect(view.activeMinute).toBeNull()
  expect(pending.at(-1).path).toBe('/bots/3/service-traffic')
  select()
  app.unmount()
  app = null
  expect(view.activeMinute).toBeNull()
  expect(listeners.has('pointerdown')).toBe(false)
})
