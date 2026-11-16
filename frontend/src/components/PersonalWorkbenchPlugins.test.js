import { expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createI18n } from 'vue-i18n'
import en from '../i18n/en.js'
import zhCN from '../i18n/zh-CN.js'
import PersonalWorkbenchPlugins from './PersonalWorkbenchPlugins.vue'

function renderPlugins(locale, props) {
  const app = createSSRApp({ render: () => h(PersonalWorkbenchPlugins, props) })
  app.use(createI18n({ legacy: false, locale, messages: { en, 'zh-CN': zhCN } }))
  app.component('ElButton', (_, { slots }) => h('button', slots.default?.()))
  app.component('ElDialog', (_, { attrs, slots }) =>
    h('section', [h('h2', attrs.title), slots.default?.(), slots.footer?.()])
  )
  return renderToString(app)
}

it.each([
  [
    'en',
    'Loaded plugins',
    'The service has loaded 2 resource reporting plugins',
    'Metrics A and Metrics B',
  ],
  ['zh-CN', '已加载插件', '服务已加载 2 个资源报表插件', 'Metrics A和Metrics B'],
])('renders scoped source names in %s', async (locale, title, count, metrics) => {
  const html = await renderPlugins(locale, {
    inventory: { resource_reporters: 2 },
    groups: [
      { source: { kind: 'identity_provider', label: 'Native only' } },
      { source: { kind: 'reporter', label: 'Reporter A' } },
      { source: { kind: 'reporter', label: 'Reporter A' } },
    ],
    metricSources: [
      { configuration: { name: 'Metrics A' } },
      { configuration: { name: 'Metrics B' } },
    ],
  })
  expect(html).toContain(title)
  expect(html).toContain(count)
  expect(html).toContain(metrics)
  expect(html.match(/Reporter A/g)).toHaveLength(1)
  expect(html).not.toContain('Native only')
})

it('distinguishes one reporter from an empty inventory', async () => {
  const props = { groups: [], metricSources: [] }
  const one = await renderPlugins('en', { ...props, inventory: { resource_reporters: 1 } })
  expect(one).toContain('loaded 1 resource reporting plugin')
  expect(one).not.toContain('loaded 1 resource reporting plugins')
  const empty = await renderPlugins('en', { ...props, inventory: { resource_reporters: 0 } })
  expect(empty).toContain('The service has no resource reporting plugins')
  expect(empty).toContain('No utilization source for resources you hold')
})
