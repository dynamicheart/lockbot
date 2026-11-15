/* eslint-disable vue/one-component-per-file -- Local stubs render the credential toolbar. */
import { expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createI18n } from 'vue-i18n'
import ConfigurationCredentialManager from './ConfigurationCredentialManager.vue'
import en from '../i18n/en'

vi.mock('../utils/api', () => ({ default: {} }))
vi.mock('../utils/helpers', () => ({ useHelpers: () => ({}) }))

async function renderManager(canProvision) {
  const app = createSSRApp(ConfigurationCredentialManager, { canProvision })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.directive('loading', {})
  app.component(
    'ElButton',
    defineComponent({
      props: { disabled: Boolean },
      setup:
        (props, { slots }) =>
        () =>
          h('button', { disabled: props.disabled }, slots.default?.()),
    })
  )
  for (const component of [
    'ElAlert',
    'ElTable',
    'ElTableColumn',
    'ElDialog',
    'ElForm',
    'ElFormItem',
    'ElInput',
    'ElRadioGroup',
    'ElRadio',
    'ElSelect',
    'ElOption',
    'ElCheckboxGroup',
    'ElCheckbox',
  ]) {
    app.component(component, { render: () => null })
  }
  return renderToString(app)
}

it('allows a Console administrator to issue the first provisioning credential without a bot', async () => {
  const admin = await renderManager(true)
  const member = await renderManager(false)
  expect(admin).toMatch(/<button(?![^>]*disabled)[^>]*>Create configuration credential<\/button>/)
  expect(member).toMatch(/<button[^>]*disabled[^>]*>Create configuration credential<\/button>/)
})
