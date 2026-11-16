import { expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createI18n } from 'vue-i18n'
import en from '../i18n/en.js'
import zhCN from '../i18n/zh-CN.js'
import PersonalAgentActivity from './PersonalAgentActivity.vue'

const locales = { en, 'zh-CN': zhCN }
const lastSeen = '2026-11-15T01:23:45Z'

function renderActivity(locale, activity) {
  const app = createSSRApp({ render: () => h(PersonalAgentActivity, { activity }) })
  app.use(createI18n({ legacy: false, locale, messages: locales }))
  return renderToString(app)
}

it.each([
  ['en', '2 active · past 15 min', 'Last source IP 192.0.2.41', 'Unnamed client', 'Last seen'],
  ['zh-CN', '近 15 分钟 · 2 个活跃', '最近来源 IP 192.0.2.41', '未命名客户端', '最后活跃'],
])(
  'renders Agent activity in %s with values intact',
  async (locale, summary, ip, unnamed, seen) => {
    const html = await renderActivity(locale, {
      minutes: 15,
      count: 2,
      items: [
        { label: 'render-worker', ip: '192.0.2.41', lastSeen },
        { label: null, ip: null, lastSeen },
      ],
    })
    for (const value of [summary, ip, unnamed, seen, 'render-worker', `datetime="${lastSeen}"`]) {
      expect(html).toContain(value)
    }
  }
)

it('uses English when the activity list is empty', async () => {
  const html = await renderActivity('en', { minutes: 15, count: 0, items: [] })
  expect(html).toContain('No requests recorded')
  expect(html).toContain('0 active · past 15 min')
})
