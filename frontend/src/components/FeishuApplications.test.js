/* eslint-disable vue/one-component-per-file -- SSR harness uses local Element Plus stubs. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createSSRApp, createRenderer, nextTick, ssrContextKey, defineComponent, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import FeishuApplications from './FeishuApplications.vue'
import api from '../utils/api'
import { ElMessage, ElMessageBox } from 'element-plus'
vi.mock('../utils/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key }) }))
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), warning: vi.fn() },
  ElMessageBox: { confirm: vi.fn() },
}))
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
afterEach(() => {
  mounted?.unmount()
  vi.useRealTimers()
})
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
it('reveals only on explicit action and clears after 20 seconds, dialog close and unmount', async () => {
  vi.useFakeTimers()
  api.get.mockResolvedValue({ data: [] })
  api.post.mockResolvedValue({ data: { app_secret: 'FAKE_SECRET_ONLY' } })
  const view = mountView()
  view.openEdit({ id: 7, name: 'Fixture', app_id: 'cli_fixture', transport: 'websocket' })
  expect(api.post).not.toHaveBeenCalled()
  await view.revealSecret()
  expect(view.revealedSecret).toBe('FAKE_SECRET_ONLY')
  await vi.advanceTimersByTimeAsync(20000)
  expect(view.revealedSecret).toBe('')
  await view.revealSecret()
  view.dialog = false
  expect(view.revealedSecret).toBe('')
  expect(view.form.app_secret).toBe('')
  view.openEdit({ id: 7, name: 'Fixture', app_id: 'cli_fixture' })
  await view.revealSecret()
  mounted.unmount()
  expect(view.revealedSecret).toBe('')
})
it('discards a late reveal when the dialog changes to another application', async () => {
  api.get.mockResolvedValue({ data: [] })
  let resolve
  api.post.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done
      })
  )
  const view = mountView()
  view.openEdit({ id: 1, name: 'One', app_id: 'cli_one' })
  const pending = view.revealSecret()
  view.openEdit({ id: 2, name: 'Two', app_id: 'cli_two' })
  resolve({ data: { app_secret: 'FAKE_STALE_SECRET' } })
  await pending
  expect(view.revealedSecret).toBe('')
  expect(view.editingId).toBe(2)
})
it('updates name, opt-in policy and only an explicitly entered replacement Secret', async () => {
  api.get.mockResolvedValue({ data: [] })
  api.patch.mockResolvedValue({ data: {} })
  const view = mountView()
  view.openEdit({ id: 7, name: 'Fixture', app_id: 'cli_fixture', transport: 'http' })
  view.formRef = { validate: () => Promise.resolve(true) }
  view.form.name = 'Renamed'
  await view.save()
  expect(api.patch).toHaveBeenLastCalledWith('/feishu/applications/7', {
    name: 'Renamed',
    unbound_greeting_enabled: false,
  })
  view.openEdit({ id: 7, name: 'Renamed', app_id: 'cli_fixture', transport: 'http' })
  view.form.app_secret = 'FAKE_REPLACEMENT'
  await view.save()
  expect(api.patch).toHaveBeenLastCalledWith('/feishu/applications/7', {
    name: 'Renamed',
    unbound_greeting_enabled: false,
    app_secret: 'FAKE_REPLACEMENT',
  })
  expect(view.form.app_secret).toBe('')
})
it('deletes only after confirmation and keeps the row when the server refuses', async () => {
  api.get.mockResolvedValue({ data: [{ id: 7, name: 'Fixture' }] })
  const view = mountView()
  ElMessageBox.confirm.mockRejectedValueOnce('cancel')
  await view.removeApplication({ id: 7, name: 'Fixture' })
  expect(api.delete).not.toHaveBeenCalled()
  ElMessageBox.confirm.mockResolvedValueOnce('confirm')
  api.delete.mockRejectedValueOnce(new Error('associated bots'))
  await expect(view.removeApplication({ id: 7, name: 'Fixture' })).rejects.toThrow(
    'associated bots'
  )
  expect(view.operations[7]).toBeUndefined()
  expect(view.applications).toHaveLength(1)
})
it('warns when saved credentials need a receiver restart', async () => {
  api.get.mockResolvedValue({ data: [] })
  api.patch.mockResolvedValue({ data: { receiver_restart_required: true } })
  const view = mountView()
  view.openEdit({ id: 7, name: 'Fixture', app_id: 'cli_fixture' })
  view.formRef = { validate: () => Promise.resolve(true) }
  view.form.app_secret = 'FAKE_REPLACEMENT'
  await view.save()
  expect(ElMessage.warning).toHaveBeenCalledWith('feishu.receiverRestartAfterUpdate')
  expect(ElMessage.success).not.toHaveBeenCalled()
})

it('defaults onboarding off and loads each application opt-in independently', async () => {
  api.get.mockResolvedValue({ data: [] })
  api.patch.mockResolvedValue({ data: {} })
  const view = mountView()
  view.openCreate()
  expect(view.form.unbound_greeting_enabled).toBe(false)
  view.openEdit({ id: 7, name: 'Enabled', app_id: 'cli_fixture', unbound_greeting_enabled: true })
  expect(view.form.unbound_greeting_enabled).toBe(true)
  view.formRef = { validate: () => Promise.resolve(true) }
  await view.save()
  expect(api.patch).toHaveBeenLastCalledWith('/feishu/applications/7', {
    name: 'Enabled',
    unbound_greeting_enabled: true,
  })
  view.openEdit({ id: 8, name: 'Other', app_id: 'cli_other' })
  expect(view.form.unbound_greeting_enabled).toBe(false)
})
