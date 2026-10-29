import { afterEach, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import ResourceDatasourceEditor from './ResourceDatasourceEditor.vue'
import { prometheusDatasource } from '../utils/utilization/prometheus'
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key) => key }) }))
vi.mock('element-plus', () => ({ ElMessage: { success: vi.fn() } }))
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
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
