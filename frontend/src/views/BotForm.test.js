import { afterEach, expect, it, vi } from 'vitest'
import { createRenderer, defineComponent, h, nextTick, reactive, ref, watch } from 'vue'
import { renderToString } from 'vue/server-renderer'
import BotForm from './BotForm.vue'
const model = vi.hoisted(() => ({
  route: null,
  clear: vi.fn(),
  validate: vi.fn(),
  create: vi.fn(),
  formProps: null,
}))
vi.mock('vue-router', () => ({
  useRoute: () => model.route,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
vi.mock('../stores/auth', () => ({ useAuthStore: () => ({ isSuperAdmin: true }) }))
vi.mock('../stores/bots', () => ({ useBotsStore: () => ({ createBot: model.create }) }))
vi.mock('../utils/api', () => ({ default: { get: vi.fn(async () => ({ data: [] })) } }))
vi.mock('element-plus', () => ({ ElMessage: { error: vi.fn(), success: vi.fn() } }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key, locale: ref('zh') }) }))
vi.mock('../components/BotForm/NodeBotForm.vue', () => ({
  default: { render: () => h('div', { 'data-test': 'resource-config' }, 'resource-config') },
}))
vi.mock('../components/BotForm/DeviceBotForm.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/BotForm/QueueBotForm.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/BotForm/ClusterPreview.vue', () => ({ default: { render: () => null } }))
vi.mock('../components/FeishuGroupInfo.vue', () => ({ default: { render: () => null } }))
const renderer = createRenderer({
  createElement: (tag) => ({ tag, children: [] }),
  createText: (text) => ({ text }),
  createComment: (text) => ({ text }),
  insert: (child, parent, anchor) => {
    child.parent = parent
    const index = parent.children.indexOf(anchor)
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child)
  },
  remove: (child) => {
    const list = child.parent?.children
    const index = list?.indexOf(child)
    if (index >= 0) list.splice(index, 1)
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
  patchProp: () => {},
})
let app
const flush = async () => {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}
afterEach(() => {
  app?.unmount()
  app = null
  vi.clearAllMocks()
})
it('switches platform without whole-form validation, clears credentials and validates on submit', async () => {
  model.route = reactive({ params: {}, query: {} })
  model.validate.mockRejectedValue({ name: [{ message: 'required' }] })
  const Form = defineComponent({
    props: {
      rules: { type: Object, default: () => ({}) },
      model: { type: Object, default: () => ({}) },
      validateOnRuleChange: { type: Boolean, default: true },
    },
    setup(props, { slots, expose }) {
      model.formProps = props
      watch(
        () => props.rules,
        () => {
          if (props.validateOnRuleChange) model.validate()
        },
        { deep: true }
      )
      expose({ clearValidate: model.clear, validate: model.validate })
      return () => h('form', slots.default?.())
    },
  })
  app = renderer.createApp(BotForm)
  app.component('ElForm', Form)
  app.directive('loading', {})
  app.config.warnHandler = () => {}
  app.config.globalProperties.$t = (key) => key
  const html = await renderToString(app)
  expect(html.indexOf('platform-connection')).toBeLessThan(html.indexOf('resource-config'))
  expect(html.indexOf('botCreate.webhookUrl')).toBeLessThan(html.indexOf('resource-config'))
  app.mount({ children: [] })
  const view = app._instance.setupState
  view.formRef = { clearValidate: model.clear, validate: model.validate }
  await flush()
  view.form.webhook_url = 'https://old.example/webhook'
  view.form.token = 'fixture-token'
  view.form.aes_key = 'fixture-key'
  view.form.group_id = 'old-group'
  view.form.platform = 'Feishu'
  await flush()
  expect(model.formProps.validateOnRuleChange).toBe(false)
  expect(model.validate).not.toHaveBeenCalled()
  expect(model.clear).toHaveBeenCalledWith([
    'webhook_url',
    'aes_key',
    'token',
    'group_id',
    'feishu_application_id',
  ])
  expect(view.form.webhook_url).toBe('')
  expect(view.form.token).toBe('')
  expect(view.form.group_id).toBe('')
  view.form.feishu_application_id = 3
  view.form.group_id = 'oc_fixture'
  view.form.platform = 'Infoflow'
  await flush()
  expect(view.form.feishu_application_id).toBeNull()
  expect(view.form.group_id).toBe('')
  await view.handleSubmit()
  expect(model.validate).toHaveBeenCalledTimes(1)
  expect(model.create).not.toHaveBeenCalled()
  model.route.params.id = '1'
  view.form.token = 'edit-preserved'
  view.form.platform = 'Feishu'
  await flush()
  expect(view.isEdit).toBe(true)
  expect(view.form.token).toBe('edit-preserved')
})
