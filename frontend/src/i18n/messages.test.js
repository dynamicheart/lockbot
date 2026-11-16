import { expect, it } from 'vitest'
import { createI18n } from 'vue-i18n'
import en from './en.js'
import zhCN from './zh-CN.js'

const locales = { en, 'zh-CN': zhCN }

function messageKeys(tree, prefix = '') {
  return Object.entries(tree).flatMap(([key, value]) => {
    const path = prefix + '[' + JSON.stringify(key) + ']'
    return typeof value === 'string' ? [path] : messageKeys(value, path)
  })
}

it('provides the same message keys in both locales', () => {
  expect(messageKeys(zhCN).sort()).toEqual(messageKeys(en).sort())
})

it.each(Object.entries(locales))(
  'renders every %s message without compilation errors',
  (locale, messages) => {
    const { t } = createI18n({
      legacy: false,
      locale,
      fallbackLocale: false,
      messages: { [locale]: messages },
    }).global

    const failures = []
    for (const key of messageKeys(messages)) {
      try {
        if (t(key) === key) failures.push(`${key}: message could not be resolved`)
      } catch (error) {
        failures.push(`${key}: ${error.message}`)
      }
    }
    expect(failures).toEqual([])
  }
)
