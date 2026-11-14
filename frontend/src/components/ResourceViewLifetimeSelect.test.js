/* eslint-disable vue/one-component-per-file -- test-only Element Plus stubs */
import { expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import ResourceViewLifetimeSelect from './ResourceViewLifetimeSelect.vue'
import zhCN from '../i18n/zh-CN'

async function renderOptions(modelValue, allowInherit = false) {
  const app = createSSRApp(ResourceViewLifetimeSelect, { modelValue, allowInherit })
  app.config.globalProperties.$t = (key, values) => {
    const message = key.split('.').reduce((catalog, part) => catalog[part], zhCN)
    return values ? message.replace('{count}', values.count) : message
  }
  const options = []
  app.component('ElSelect', {
    props: { modelValue: { type: [Number, String], required: true } },
    render() {
      return h('div', { 'data-selected': this.modelValue }, this.$slots.default())
    },
  })
  app.component('ElOption', {
    props: {
      value: { type: [Number, String], required: true },
      label: { type: String, required: true },
    },
    render() {
      options.push([this.value, this.label])
      return h('span', this.label)
    },
  })
  return { html: await renderToString(app), options }
}

it('shows six site presets with permanent selected by default', async () => {
  const { html, options } = await renderOptions(0)
  expect(html).toContain('data-selected="0"')
  expect(options).toEqual([
    [1, '1 天'],
    [7, '7 天'],
    [30, '1 个月'],
    [90, '3 个月'],
    [180, '半年'],
    [0, '永久有效'],
  ])
})

it('shows the inherited selection in the bot form', async () => {
  const { html, options } = await renderOptions('inherit', true)
  expect(html).toContain('data-selected="inherit"')
  expect(options[0]).toEqual(['inherit', '继承平台'])
  expect(options).toHaveLength(7)
})

it('retains the label and value of a saved day count outside the presets', async () => {
  const { html, options } = await renderOptions(12, true)
  expect(html).toContain('data-selected="12"')
  expect(options).toContainEqual([12, '12 天'])
  expect(options).toHaveLength(8)
})
