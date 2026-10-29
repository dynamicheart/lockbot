/* eslint-disable vue/one-component-per-file -- SSR harness uses local Element Plus stubs. */
import { expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import FeishuApplications from './FeishuApplications.vue'
import api from '../utils/api'
vi.mock('../utils/api', () => ({ default: { get: vi.fn(), post: vi.fn() } }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key }) }))
vi.mock('element-plus', () => ({ ElMessage: { success: vi.fn() } }))
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
