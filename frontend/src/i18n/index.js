import { createI18n } from 'vue-i18n'
import en from './en.js'
import zhCN from './zh-CN.js'

const LOCALE_KEY = 'lockbot_locale'

const i18n = createI18n({
  legacy: false,
  locale: localStorage.getItem(LOCALE_KEY) || 'zh-CN',
  fallbackLocale: 'en',
  messages: {
    en,
    'zh-CN': zhCN,
  },
})

export function setLocale(locale) {
  i18n.global.locale.value = locale
  localStorage.setItem(LOCALE_KEY, locale)
}

export function getLocale() {
  return i18n.global.locale.value
}

export default i18n
