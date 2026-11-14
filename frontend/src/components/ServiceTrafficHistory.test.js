/* eslint-disable vue/one-component-per-file -- minimal host stubs exercise actual SFC bindings */
import { afterEach, expect, it, vi } from 'vitest'
import * as Vue from 'vue'
import { createRenderer, defineComponent, h, nextTick, reactive } from 'vue'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { readFileSync } from 'node:fs'
import Panel from './ServiceTrafficHistory.vue'
import api from '../utils/api'
vi.mock('../utils/api', () => ({ default: { get: vi.fn() } }))
const mocks = vi.hoisted(() => ({ locale: { value: 'en' } }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key, locale: mocks.locale }) }))
const filename = new URL('./ServiceTrafficHistory.vue', import.meta.url).pathname
const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
const bindings = compileScript(descriptor, { id: 'history-test' }).bindings
const { code } = compileTemplate({
  source: descriptor.template.content,
  filename,
  id: 'history-test',
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
    const index = node.parent?.children.indexOf(node) ?? -1
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
const shell = defineComponent({
  setup:
    (_props, { slots }) =>
    () =>
      h('section', {}, [slots.header?.(), slots.default?.()]),
})
let app
const flush = async () => {
  await Promise.resolve()
  await Promise.resolve()
  await nextTick()
}
function mount() {
  const props = reactive({ botId: 4, admin: false })
  const root = { children: [] }
  app = renderer.createApp({ render: () => h(Panel, props) })
  app.provide(Vue.ssrContextKey, {})
  app.config.globalProperties.$t = (key, values) =>
    key === 'trafficHistory.inputZone' ? values.zone : key
  for (const name of [
    'el-radio-button',
    'el-button',
    'el-date-picker',
    'el-alert',
    'el-card',
    'el-tooltip',
    'el-empty',
    'el-icon',
  ])
    app.component(name, shell)
  app.component(
    'ElRadioGroup',
    defineComponent({
      name: 'HistoryRangeStub',
      emits: ['update:modelValue'],
      setup:
        (_props, { emit, slots }) =>
        () =>
          h('section', {}, [
            h(
              'button',
              { 'data-custom': true, onClick: () => emit('update:modelValue', 'custom') },
              'custom'
            ),
            slots.default?.(),
          ]),
    })
  )
  app.mount(root)
  return { props, root }
}
const all = (node) => [node, ...(node.children || []).flatMap(all)]
const text = (node) =>
  all(node)
    .map((item) => item.text || '')
    .join(' ')
    .replace(/\s+/g, ' ')
const payload = (requests = 0, coverage = 'complete') => ({
  storage: { healthy: true },
  resolution_seconds: 900,
  totals: { requests, client_errors: 0, server_errors: 0, avg_ms: 0, p95_ms: 0 },
  coverage: { complete_buckets: 1, partial_buckets: 0, unknown_buckets: 0 },
  history: [
    {
      at: '2026-11-14T01:00:00Z',
      coverage,
      observed: coverage !== 'unknown',
      requests: coverage === 'unknown' ? null : requests,
      server_errors: coverage === 'unknown' ? null : 0,
      sources: coverage === 'unknown' ? null : { agent: requests },
      operations: coverage === 'unknown' ? null : { chat: 0, agent: 0, operator: 0 },
    },
  ],
})
afterEach(() => {
  app?.unmount()
  mocks.locale.value = 'en'
  vi.clearAllMocks()
})
it('renders confirmed zero as zero and unknown history as gaps, not false zeros', async () => {
  api.get.mockResolvedValueOnce({ data: payload(0) })
  const { root, props } = mount()
  await flush()
  expect(text(root)).toContain('trafficHistory.metrics.requests 0')
  expect(
    all(root).filter((node) => node.props?.class?.split?.(' ').includes('is-unknown'))
  ).toHaveLength(0)
  api.get.mockResolvedValueOnce({ data: payload(0, 'unknown') })
  props.botId = 5
  await flush()
  expect(text(root)).toContain('trafficHistory.metrics.requests trafficHistory.notCollected')
  expect(
    all(root).filter((node) => node.props?.class?.split?.(' ').includes('is-unknown'))
  ).toHaveLength(2)
  expect(api.get.mock.calls[1][0]).toBe('/bots/5/service-traffic/history')
})
it('clears old results immediately on a bot switch and never publishes a late response', async () => {
  const pending = []
  api.get.mockImplementation(
    (url, options) => new Promise((resolve) => pending.push({ url, options, resolve }))
  )
  const { props, root } = mount()
  props.botId = 7
  await flush()
  expect(pending[0].options.signal.aborted).toBe(true)
  pending[1].resolve({ data: payload(7) })
  await flush()
  expect(text(root)).toContain('trafficHistory.metrics.requests 7')
  expect(text(root)).toContain('serviceTraffic.sources.agent 7')
  pending[0].resolve({ data: payload(444) })
  await flush()
  expect(text(root)).not.toContain('444')
})

it('shows the custom input browser timezone and clears the previous range', async () => {
  api.get.mockResolvedValueOnce({ data: payload(12) })
  const { root } = mount()
  await flush()
  all(root)
    .find((node) => node.props?.['data-custom'])
    .props.onClick()
  await flush()
  const inputZone = Intl.DateTimeFormat().resolvedOptions().timeZone
  expect(all(root).some((node) => node.props?.['aria-label'] === inputZone)).toBe(true)
  expect(text(root)).not.toContain(inputZone)
  expect(text(root)).toContain('trafficHistory.invalidRange')
  expect(text(root)).not.toContain('trafficHistory.metrics.requests')
  expect(api.get).toHaveBeenCalledTimes(1)
})
it('uses the browser timezone for history axes independently of UI language', async () => {
  const resolved = new Intl.DateTimeFormat().resolvedOptions()
  const zone = vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions').mockReturnValue({
    ...resolved,
    timeZone: 'America/Los_Angeles',
  })
  try {
    mocks.locale.value = 'zh'
    api.get.mockResolvedValueOnce({ data: payload(1) })
    const { root } = mount()
    await flush()
    for (const tick of all(root).filter((node) => node.tag === 'time')) {
      expect(tick.text).toContain('17:00')
      expect(tick.props.title).toBe('2026-11-13 17:00:00 UTC-08:00')
    }
    expect(all(root).filter((node) => node.tag === 'time')).toHaveLength(2)
    mocks.locale.value = 'en'
    await flush()
    for (const tick of all(root).filter((node) => node.tag === 'time')) {
      expect(tick.text).toContain('17:00')
      expect(tick.props.title).toBe('2026-11-13 17:00:00 UTC-08:00')
    }
    expect(all(root).filter((node) => node.tag === 'time')).toHaveLength(2)
  } finally {
    zone.mockRestore()
  }
})

it('keeps partial coverage in the point hint without a chart-top marker', async () => {
  api.get.mockResolvedValueOnce({ data: payload(1, 'partial') })
  const { root } = mount()
  await flush()
  const bars = all(root).filter((node) => node.props?.class === 'history-bar')
  expect(bars).toHaveLength(2)
  for (const bar of bars) expect(bar.props['aria-label']).toContain('trafficHistory.partial')
  expect(text(root)).not.toContain('trafficHistory.coverage')
})
