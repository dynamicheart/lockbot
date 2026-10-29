/* eslint-disable vue/one-component-per-file -- SSR table stubs render the real task-cell slot. */
import { afterEach, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import AgentHistory from './AgentHistory.vue'

vi.mock('../utils/api', () => ({ default: { get: vi.fn(async () => ({ data: { items: [] } })) } }))
vi.mock('vue-i18n', () => ({ useI18n: () => ({ locale: { value: 'zh-CN' } }) }))

let app
afterEach(() => {
  app = null
})
async function taskText(row) {
  app = createSSRApp(AgentHistory, { botId: 1, includePrivate: true })
  app.config.warnHandler = () => {}
  app.config.globalProperties.$t = (key) =>
    ({
      'agentHistory.agentLabel': 'Agent：',
      'agentHistory.noteLabel': '备注：',
    })[key] || key
  app.directive('loading', {})
  app.component(
    'ElTable',
    defineComponent({
      setup:
        (_, { slots }) =>
        () =>
          h('table', slots.default?.()),
    })
  )
  app.component(
    'ElTableColumn',
    defineComponent({
      props: { label: { type: String, default: '' } },
      setup:
        (props, { slots }) =>
        () =>
          props.label === 'agentHistory.task' ? h('td', slots.default?.({ row })) : null,
    })
  )
  const html = await renderToString(app)
  const cell = html.match(/<td[^>]*>([\s\S]*?)<\/td>/)?.[1]
  expect(cell).toBeDefined()
  return cell.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]+>/g, '')
}
it('renders the category and distinct labeled Agent and note lines without an empty marker', async () => {
  const text = await taskText({
    public_task: '算子测试',
    agent_label: 'fixture-agent',
    purpose: '验证功能',
  })
  expect(text).toContain('算子测试')
  expect(text).toContain('Agent： fixture-agent')
  expect(text).toContain('备注： 验证功能')
  expect(text).not.toContain('—')
})
it('renders only actual details when a category is missing', async () => {
  expect(await taskText({ purpose: '验证功能' })).toBe('备注： 验证功能')
})
it('renders one empty marker when the row contains no task fields', async () => {
  expect(await taskText({})).toBe('—')
})
