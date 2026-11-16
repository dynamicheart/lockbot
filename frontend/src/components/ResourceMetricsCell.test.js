import { expect, it } from 'vitest'
import { createSSRApp } from 'vue'
import { createI18n } from 'vue-i18n'
import { renderToString } from 'vue/server-renderer'
import ResourceMetricsCell from './ResourceMetricsCell.vue'
import en from '../i18n/en'
import zh from '../i18n/zh-CN'

const now = Date.parse('2026-11-15T08:00:00Z')

async function renderSample(locale, expiresAt) {
  const app = createSSRApp(ResourceMetricsCell, {
    nodeId: 'example',
    now,
    reportedSample: { compute_percent: 0, memory_percent: null, expires_at: expiresAt },
  })
  app.component('ElPopover', { render: () => null })
  app.use(createI18n({ legacy: false, locale, messages: { en, 'zh-CN': zh } }))
  return renderToString(app)
}

it.each([
  ['en', 'Compute 0%', 'Memory Unavailable', 'No recent data'],
  ['zh-CN', '计算利用率 0%', '显存 暂不可用', '暂无最新数据'],
])(
  'renders zero, missing, and expired reporter samples in %s',
  async (locale, zero, missing, stale) => {
    const fresh = await renderSample(locale, '2026-11-15T08:00:30Z')
    expect(fresh).toContain(zero)
    expect(fresh).toContain(missing)
    const expired = await renderSample(locale, '2026-11-15T08:00:00Z')
    expect(expired).toContain(stale)
    expect(expired).not.toContain('0%')
  }
)
