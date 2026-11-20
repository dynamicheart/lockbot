/* eslint-disable vue/one-component-per-file -- Editor harness uses local Element Plus stubs. */
import { afterEach, expect, it, vi } from 'vitest'
import {
  createSSRApp,
  createRenderer,
  defineComponent,
  getCurrentInstance,
  h,
  nextTick,
  ssrContextKey,
} from 'vue'
import { renderToString } from 'vue/server-renderer'
import ResourceDatasourceEditor from './ResourceDatasourceEditor.vue'
import {
  DEFAULT_CONFIG,
  prometheusDatasource,
  validateConfig,
} from '../utils/utilization/prometheus'
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key }) }))
vi.mock('element-plus', () => ({ ElMessage: { success: vi.fn() } }))
afterEach(() => {
  mounted?.unmount()
  mounted = null
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const renderer = createRenderer({
  createElement: (tag) => ({ tag, children: [] }),
  createText: (text) => ({ text }),
  createComment: (text) => ({ text }),
  insert: (child, parent) => {
    child.parent = parent
    parent.children.push(child)
  },
  remove: () => {},
  setText: () => {},
  setElementText: () => {},
  parentNode: (node) => node.parent,
  nextSibling: () => null,
  patchProp: () => {},
})
let mounted
function savedConfig(extra = {}) {
  return validateConfig({
    ...DEFAULT_CONFIG,
    profile: 'lockbot-v1',
    endpoint: 'https://metrics.example.test',
    nodeMapping: { 'fixture-node': 'source-node' },
    ...extra,
  })
}
function mountEditor(modelValue = savedConfig({ deviceQuery: 'exact-v1' })) {
  vi.stubGlobal('document', {
    hidden: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })
  const metric = { value: 0, observedAt: Date.now() }
  const probe = vi.spyOn(prometheusDatasource, 'testDatasource').mockResolvedValue({
    cards: [{ node: 'source-node', device: 'card-a', metrics: { compute: metric } }],
    hosts: [{ node: 'source-node', cpu: metric, used: metric, total: metric, filesystems: [] }],
  })
  const save = vi.fn()
  const emit = vi.fn()
  mounted = renderer.createApp(ResourceDatasourceEditor, {
    modelValue,
    nodes: ['fixture-node'],
    save,
    'onUpdate:modelValue': emit,
  })
  mounted.provide(ssrContextKey, { modules: new Set() })
  mounted.config.warnHandler = () => {}
  mounted.config.globalProperties.$t = (key) => key
  mounted.component('ElDialog', { render: () => null })
  mounted.mount({ children: [] })
  const view = mounted._instance.setupState
  return { view, save, emit, probe }
}

it('preserves the server declaration during opening and saves it without inferring support', async () => {
  const { view, save, emit, probe } = mountEditor()
  view.openSettings()
  await nextTick()
  expect(view.draft.deviceQuery).toBe('exact-v1')
  expect(view.exactDeviceQuery).toBe(true)
  expect(probe).not.toHaveBeenCalled()
  expect(save).not.toHaveBeenCalled()
  await view.testAndEnable()
  expect(save).toHaveBeenCalledWith(savedConfig({ deviceQuery: 'exact-v1' }))
  expect(emit).toHaveBeenCalledWith(savedConfig({ deviceQuery: 'exact-v1' }))
})

it('clears the declaration on a user URL edit and permits deliberate re-establishment', async () => {
  const { view, save } = mountEditor()
  view.openSettings()
  await nextTick()
  view.draft.endpoint = 'https://replacement.example.test'
  view.clearDeviceQuery()
  expect(view.draft).not.toHaveProperty('deviceQuery')
  await nextTick()
  view.exactDeviceQuery = true
  expect(view.draft.deviceQuery).toBe('exact-v1')
  await view.testAndEnable()
  expect(save.mock.calls[0][0]).toMatchObject({
    endpoint: 'https://replacement.example.test',
    deviceQuery: 'exact-v1',
  })
})

it.each(['custom', 'dcgm'])('clears the declaration when the user selects %s', async (preset) => {
  const { view } = mountEditor()
  view.openSettings()
  await nextTick()
  view.preset = preset
  view.applyPreset()
  expect(view.draft.profile).toBe('custom')
  expect(view.draft).not.toHaveProperty('deviceQuery')
  view.preset = 'lockbot-v1'
  view.applyPreset()
  expect(view.exactDeviceQuery).toBe(false)
  expect(view.draft).not.toHaveProperty('deviceQuery')
})

it('saves an explicit clear with the field absent', async () => {
  const { view, save } = mountEditor()
  view.openSettings()
  await nextTick()
  view.exactDeviceQuery = false
  await view.testAndEnable()
  expect(save).toHaveBeenCalledWith(savedConfig())
  expect(JSON.stringify(save.mock.calls[0][0])).not.toContain('deviceQuery')
})

it('does not leak an unsaved declaration into a later legacy configuration load', async () => {
  const { view, save } = mountEditor(savedConfig())
  view.openSettings()
  view.exactDeviceQuery = true
  view.settings = false
  await nextTick()
  view.openSettings()
  expect(view.draft).not.toHaveProperty('deviceQuery')
  await view.testAndEnable()
  expect(save).toHaveBeenCalledWith(savedConfig())
})

it.each([null, true, 'exact-v2'])(
  'rejects invalid hydrated declaration %s before source I/O',
  async (deviceQuery) => {
    const { view, save, probe } = mountEditor({ ...savedConfig(), deviceQuery })
    view.openSettings()
    await view.testAndEnable()
    expect(view.configError).toBe('configuration')
    expect(probe).not.toHaveBeenCalled()
    expect(save).not.toHaveBeenCalled()
  }
)
it('binds user endpoint input and the explicit declaration control', async () => {
  vi.stubGlobal('document', { addEventListener: vi.fn(), removeEventListener: vi.fn() })
  const app = createSSRApp(ResourceDatasourceEditor, {
    modelValue: savedConfig({ deviceQuery: 'exact-v1' }),
    nodes: ['fixture-node'],
  })
  app.config.warnHandler = () => {}
  app.config.globalProperties.$t = (key) => key
  let view, endpoint, checkbox, presetSelect
  const Container = {
    setup:
      (_, { slots }) =>
      () =>
        h('div', slots.default?.()),
  }
  app.component('ElDialog', {
    setup(_, { slots }) {
      view = getCurrentInstance().parent.setupState
      view.openSettings()
      return () => h('div', slots.default?.())
    },
  })
  for (const name of ['ElForm', 'ElFormItem']) app.component(name, Container)
  app.component('ElSelect', {
    setup(_, { attrs }) {
      if (attrs.modelValue === 'lockbot-v1') presetSelect = attrs
      return () => null
    },
  })
  app.component('ElInput', {
    setup(_, { attrs }) {
      if (attrs.placeholder === 'https://prometheus.example.com') endpoint = attrs
      return () => null
    },
  })
  app.component('ElCheckbox', {
    setup(_, { attrs, slots }) {
      checkbox = attrs
      return () => h('span', slots.default?.())
    },
  })
  const html = await renderToString(app)
  expect(html).toContain('utilization.deviceQueryExact')
  expect(checkbox.modelValue).toBe(true)
  endpoint['onUpdate:modelValue']('https://replacement.example.test')
  endpoint.onInput('https://replacement.example.test')
  expect(view.draft).not.toHaveProperty('deviceQuery')
  checkbox['onUpdate:modelValue'](true)
  expect(view.draft.deviceQuery).toBe('exact-v1')
  checkbox['onUpdate:modelValue'](false)
  expect(view.draft).not.toHaveProperty('deviceQuery')
  checkbox['onUpdate:modelValue'](true)
  presetSelect['onUpdate:modelValue']('custom')
  presetSelect.onChange('custom')
  expect(view.draft.profile).toBe('custom')
  expect(view.draft).not.toHaveProperty('deviceQuery')
})
it('keeps common connection and mapping visible, advanced collapsed, and makes no source calls', async () => {
  vi.stubGlobal('document', { addEventListener: vi.fn(), removeEventListener: vi.fn() })
  const probe = vi.spyOn(prometheusDatasource, 'testDatasource').mockResolvedValue({})
  const save = vi.fn(),
    authorize = vi.fn()
  const app = createSSRApp(ResourceDatasourceEditor, {
    nodes: ['fixture-node'],
    resources: [],
    save,
    authorize,
  })
  app.config.warnHandler = () => {}
  app.config.globalProperties.$t = (key) => key
  const Container = defineComponent({
    setup:
      (_, { slots }) =>
      () =>
        h('div', [slots.default?.(), slots.footer?.()]),
  })
  for (const name of ['ElDialog', 'ElForm', 'ElFormItem', 'ElSelect', 'ElButton'])
    app.component(name, Container)
  const html = await renderToString(app)
  expect(html).toContain('utilization.notConfigured')
  expect(html).toContain('utilization.configureSource')
  expect(html).not.toContain('utilization.title')
  expect(html).toContain('utilization.connectionSection')
  expect(html).toContain('utilization.mappingSection')
  expect(html).toMatch(/<details class="advanced-settings"[^>]*>/)
  expect(html).not.toMatch(/<details[^>]*class="advanced-settings"[^>]*open/)
  expect(html).toContain('utilization.testSave')
  expect(save).not.toHaveBeenCalled()
  expect(authorize).not.toHaveBeenCalled()
  expect(probe).not.toHaveBeenCalled()
})
