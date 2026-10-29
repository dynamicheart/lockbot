import { expect, it } from 'vitest'
import { platformLabel } from './platformDisplay'
import en from '../i18n/en'
import zh from '../i18n/zh-CN'
it('uses the same bilingual platform names without changing protocol values', () => {
  for (const messages of [en, zh]) {
    const t = (key) => key.split('.').reduce((value, part) => value[part], messages)
    expect(platformLabel('Infoflow', t)).toBe('如流 / Infoflow')
    expect(platformLabel('Feishu', t)).toBe('飞书 / Feishu')
    expect(t('feishu.groupHelp')).toContain('oc_')
  }
  expect(platformLabel('custom', () => '')).toBe('custom')
})
