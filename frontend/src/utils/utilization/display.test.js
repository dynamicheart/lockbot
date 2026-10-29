import { describe, it, expect } from 'vitest'
import { loadLevel, sourceResources } from './display'
import { metricStatus } from './prometheus'
const now = 1800000000000
const card = (value) => ({
  metrics: Object.fromEntries(
    ['compute', 'used', 'total'].map((name) => [name, { value, observedAt: now }])
  ),
})
describe('utilization presentation', () => {
  it('colors measured load without treating missing/stale data as idle', () => {
    expect(loadLevel([card(0)], now, 90, metricStatus)).toBe('low')
    expect(loadLevel([card(30)], now, 90, metricStatus)).toBe('medium')
    expect(loadLevel([card(80)], now, 90, metricStatus)).toBe('high')
    expect(loadLevel([], now, 90, metricStatus)).toBe('unknown')
    expect(loadLevel([card(0)], now + 91000, 90, metricStatus)).toBe('unknown')
    const partial = card(0)
    partial.metrics.compute.value = null
    expect(loadLevel([partial], now, 90, metricStatus)).toBe('unknown')
  })
  it('uses the existing cluster schema for exact card mappings', () => {
    expect(
      sourceResources([{ node_key: 'node-a', devices: ['h20', 'h20'] }], 'QUEUE_DEVICE')
    ).toEqual({ nodes: ['node-a'], resources: ['node-a:dev0', 'node-a:dev1'] })
    expect(sourceResources({ 'node-a': ['h20'] }, 'DEVICE').resources).toEqual(['node-a:dev0'])
    expect(sourceResources([null, {}, { node_key: 'draft', devices: {} }], 'DEVICE')).toEqual({
      nodes: ['draft'],
      resources: [],
    })
    expect(sourceResources(['node-a'], 'QUEUE')).toEqual({ nodes: ['node-a'], resources: [] })
  })
})
