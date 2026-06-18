import { describe, it, expect } from 'vitest'
import { orderedEntries, orderedStringify, isDeviceArrayFormat, getNodeOrder } from './helpers.js'

describe('isDeviceArrayFormat', () => {
  it('returns true for [{node_key, devices}] format', () => {
    expect(isDeviceArrayFormat([{ node_key: '0', devices: ['a800'] }])).toBe(true)
  })
  it('returns false for plain string array (NODE/QUEUE)', () => {
    expect(isDeviceArrayFormat(['n1', 'n2'])).toBe(false)
  })
  it('returns false for empty array', () => {
    expect(isDeviceArrayFormat([])).toBe(false)
  })
  it('returns false for dict', () => {
    expect(isDeviceArrayFormat({ 0: ['a800'] })).toBe(false)
  })
  it('returns false for null/undefined', () => {
    expect(isDeviceArrayFormat(null)).toBe(false)
    expect(isDeviceArrayFormat(undefined)).toBe(false)
  })
})

describe('getNodeOrder', () => {
  it('extracts order from DEVICE array format', () => {
    const cc = [
      { node_key: '2', devices: ['a800'] },
      { node_key: '0', devices: ['h20'] },
      { node_key: '1', devices: [] },
    ]
    expect(getNodeOrder(cc)).toEqual(['2', '0', '1'])
  })
  it('passes through NODE/QUEUE string array as-is', () => {
    expect(getNodeOrder(['n3', 'n1', 'n2'])).toEqual(['n3', 'n1', 'n2'])
  })
  it('falls back to Object.keys for legacy dict', () => {
    // JS sorts numeric keys, so Object.keys({2:..., 0:..., 1:...}) = ["0","1","2"]
    expect(getNodeOrder({ 2: ['a100'], 0: ['a800'], 1: ['h20'] })).toEqual(['0', '1', '2'])
  })
  it('returns empty array for null', () => {
    expect(getNodeOrder(null)).toEqual([])
  })
})

describe('orderedEntries', () => {
  it('preserves array config order for numeric keys', () => {
    const data = { 0: 'a', 1: 'b', 2: 'c' }
    const config = ['2', '0', '1']
    const keys = orderedEntries(data, config).map(([k]) => k)
    expect(keys).toEqual(['2', '0', '1'])
  })

  it('preserves DEVICE array format order (new format)', () => {
    const data = { 0: ['a800'], 1: ['h20'], 2: ['a100'] }
    const config = [
      { node_key: '2', devices: ['a100'] },
      { node_key: '0', devices: ['a800'] },
      { node_key: '1', devices: ['h20'] },
    ]
    const keys = orderedEntries(data, config).map(([k]) => k)
    expect(keys).toEqual(['2', '0', '1'])
  })

  it('preserves dict config order for numeric keys (DEVICE legacy)', () => {
    const data = { 0: ['A800'], 1: ['H20'], 2: ['A100'] }
    const config = { 2: ['A100'], 0: ['A800'], 1: ['H20'] }
    // Object.keys on config will be sorted as "0","1","2" by JS engine
    // so orderedEntries uses that order — this tests the real behavior
    const keys = orderedEntries(data, config).map(([k]) => k)
    expect(keys).toEqual(['0', '1', '2'])
  })

  it('case-insensitive matching', () => {
    const data = { GPU01: 'locked', gpu02: 'free' }
    const config = ['gpu01', 'GPU02']
    const result = orderedEntries(data, config)
    expect(result.map(([k]) => k)).toEqual(['GPU01', 'gpu02'])
  })

  it('appends keys not in config at the end', () => {
    const data = { a: 1, b: 2, c: 3 }
    const config = ['b']
    const keys = orderedEntries(data, config).map(([k]) => k)
    expect(keys[0]).toBe('b')
    expect(keys).toContain('a')
    expect(keys).toContain('c')
  })

  it('returns empty array for null/undefined data', () => {
    expect(orderedEntries(null, ['a'])).toEqual([])
    expect(orderedEntries(undefined, ['a'])).toEqual([])
  })

  it('falls back to Object.entries when no config', () => {
    const data = { x: 1, y: 2 }
    expect(orderedEntries(data, null)).toEqual(Object.entries(data))
  })

  it('mixed case letters and numbers', () => {
    // Simulates: config has mixed-case names + numeric keys
    const data = { 3: 'idle', NodeA: 'locked', nodeb: 'free', 1: 'locked', GPU02: 'free' }
    const config = ['NodeA', 'NODEB', '3', 'gpu02', '1']
    const keys = orderedEntries(data, config).map(([k]) => k)
    expect(keys).toEqual(['NodeA', 'nodeb', '3', 'GPU02', '1'])
  })

  it('real-world DEVICE config with numeric + alpha keys', () => {
    const data = { 0: ['A800'], 1: ['H20'], NodeX: ['A100'], 2: ['V100'] }
    const config = ['NodeX', '2', '0', '1']
    const keys = orderedEntries(data, config).map(([k]) => k)
    expect(keys).toEqual(['NodeX', '2', '0', '1'])
  })
})

describe('orderedStringify', () => {
  it('produces JSON with keys in config order', () => {
    const data = { 0: 'a', 1: 'b', 2: 'c' }
    const config = ['2', '0', '1']
    const json = orderedStringify(data, config)
    const keyOrder = [...json.matchAll(/"(\d+)":/g)].map((m) => m[1])
    expect(keyOrder).toEqual(['2', '0', '1'])
  })

  it('output is valid JSON (parseable)', () => {
    const data = { 0: ['A800', 'H20'], 1: ['A100'] }
    const config = ['1', '0']
    const json = orderedStringify(data, config)
    const parsed = JSON.parse(json)
    expect(parsed).toEqual(data)
  })

  it('re-parsed JSON loses order (demonstrates the problem)', () => {
    const data = { 2: 'c', 0: 'a', 1: 'b' }
    const config = ['2', '0', '1']
    const json = orderedStringify(data, config)
    const reparsed = JSON.parse(json)
    // JS re-sorts numeric keys
    expect(Object.keys(reparsed)).toEqual(['0', '1', '2'])
    // But orderedEntries restores correct order
    const restored = orderedEntries(reparsed, config).map(([k]) => k)
    expect(restored).toEqual(['2', '0', '1'])
  })
})
