/* eslint-disable vue/one-component-per-file -- SSR harness uses local Element Plus stubs. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createSSRApp, createRenderer, nextTick, ssrContextKey, defineComponent, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import FeishuApplications from './FeishuApplications.vue'
import api from '../utils/api'
vi.mock('../utils/api', () => ({ default: { get: vi.fn(), post: vi.fn() } }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key }) }))
vi.mock('element-plus', () => ({ ElMessage: { success: vi.fn() } }))
beforeEach(() => vi.clearAllMocks())
it('renders field help and setup guide while keeping secret masked and rule-change validation off', async () => {
  const app = createSSRApp(FeishuApplications)
  app.directive('loading', {})
  app.config.warnHandler = () => {}
  app.config.globalProperties.$t = (key) => key
  let ruleChange
  const Container = defineComponent({
    setup:
      (_, { slots }) =>
      () =>
        h('div', slots.default?.()),
  })
  app.component('ElTable', { render: () => null })
  app.component('ElDialog', Container)
  app.component(
    'ElForm',
    defineComponent({
      props: { validateOnRuleChange: { type: Boolean, default: true } },
      setup(props, { slots }) {
        ruleChange = props.validateOnRuleChange
        return () => h('form', slots.default?.())
      },
    })
  )
  app.component(
    'ElFormItem',
    defineComponent({
      setup:
        (_, { slots }) =>
        () =>
          h('div', [slots.label?.(), slots.default?.()]),
    })
  )
  app.component(
    'ElTooltip',
    defineComponent({
      props: { content: { type: String, default: '' } },
      setup: (props) => () => h('span', props.content),
    })
  )
  const html = await renderToString(app)
  for (const key of ['nameHelp', 'appIdHelp', 'appSecretHelp', 'transportHelp', 'setupGuide'])
    expect(html).toContain('feishu.' + key)
  expect(html).toContain('docs/usage/feishu.md')
  expect(html).toMatch(/type="password"/)
  expect(ruleChange).toBe(false)
  expect(api.post).not.toHaveBeenCalled()
  expect(api.get).not.toHaveBeenCalled()
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
function mountView() {
  mounted = renderer.createApp(FeishuApplications)
  mounted.provide(ssrContextKey, { modules: new Set() })
  mounted.config.warnHandler = () => {}
  mounted.config.globalProperties.$t = (key) => key
  mounted.directive('loading', {})
  mounted.component('ElTable', { render: () => null })
  mounted.component('ElDialog', { render: () => null })
  mounted.mount({ children: [] })
  return mounted._instance.setupState
}
afterEach(() => mounted?.unmount())
it('refreshes a running receiver using GET only and starts a stopped receiver once', async () => {
  vi.useFakeTimers()
  const running = { id: 1, transport: 'websocket', receiver: { state: 'running' } }
  api.get.mockResolvedValue({ data: [running] })
  api.post.mockResolvedValue({ data: { started: true } })
  const view = mountView()
  await nextTick()
  await view.receiverAction(running)
  expect(api.post).not.toHaveBeenCalled()
  expect(api.get).toHaveBeenLastCalledWith('/feishu/applications')
  expect(view.receiverState(running)).toBe('running')
  const stopped = { id: 2, transport: 'websocket', receiver: { state: 'stopped' } }
  let release
  api.post.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        release = resolve
      })
  )
  const start = view.receiverAction(stopped)
  expect(view.operations[2]).toBe(true)
  await view.receiverAction(stopped)
  expect(api.post).toHaveBeenCalledTimes(1)
  expect(api.post).toHaveBeenCalledWith('/feishu/applications/2/connect')
  release({ data: { started: true } })
  await start
  expect(view.operations[2]).toBeUndefined()
  const count = api.get.mock.calls.length
  await vi.advanceTimersByTimeAsync(60000)
  expect(api.get).toHaveBeenCalledTimes(count)
  vi.useRealTimers()
})
it('clears action loading when status refresh fails and treats missing status as stopped', async () => {
  api.get.mockResolvedValueOnce({ data: [] })
  const view = mountView()
  await nextTick()
  api.get.mockRejectedValueOnce(new Error('unavailable'))
  await expect(view.receiverAction({ id: 3, receiver: { state: 'retrying' } })).rejects.toThrow(
    'unavailable'
  )
  expect(view.operations[3]).toBeUndefined()
  expect(api.post).not.toHaveBeenCalled()
  expect(view.receiverState({})).toBe('stopped')
})
