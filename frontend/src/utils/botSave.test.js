import { expect, it } from 'vitest'
import { changedConfig } from './botSave'

it('ignores JSON object key order, preserving resource array order', () => {
  expect(
    changedConfig({ config_overrides: { B: 2, A: 1 } }, { config_overrides: { A: 1, B: 2 } })
  ).toEqual({})
  expect(changedConfig({ cluster_configs: ['b', 'a'] }, { cluster_configs: ['a', 'b'] })).toEqual({
    cluster_configs: ['b', 'a'],
  })
})
it('identifies only edited config fields, so an authorization-only save needs no restart', () => {
  const original = { name: 'bot', config_overrides: { LANGUAGE: 'zh' } }
  expect(changedConfig(structuredClone(original), original)).toEqual({})
  expect(changedConfig({ ...original, name: 'renamed' }, original)).toEqual({ name: 'renamed' })
})
