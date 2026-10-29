import { describe, it, expect, vi } from 'vitest'
import {
  DEFAULT_CONFIG,
  validateConfig,
  collectPrometheus,
  metricStatus,
  resolveCards,
  resolveHost,
} from './prometheus'
const now = 1800000000000
const config = () =>
  validateConfig({
    ...DEFAULT_CONFIG,
    endpoint: 'https://metrics.example.invalid',
    profile: 'lockbot-v1',
    nodeMapping: { clusterNode: 'host-a' },
    deviceMapping: { 'clusterNode:dev1': '1' },
    intervalSeconds: 60,
    staleSeconds: 660,
  })
const sample = (name, value, labels = {}) => ({
  metric: { __name__: name, node: 'host-a', ...labels },
  value: [now / 1000, String(value)],
})
function payload({ age = 300, success = 1 } = {}) {
  return [
    ...['accelerator', 'host'].flatMap((collector) => [
      sample('lockbot_sample_timestamp_seconds', now / 1000 - age, { collector }),
      sample('lockbot_collector_success', success, { collector }),
    ]),
    ...['0', '1'].flatMap((device) => [
      sample('lockbot_accelerator_utilization_percent', device === '0' ? 0 : 92, { device }),
      sample('lockbot_accelerator_memory_used_bytes', 2 * 1024 ** 3, { device }),
      sample('lockbot_accelerator_memory_total_bytes', 16 * 1024 ** 3, { device }),
    ]),
    sample('lockbot_host_cpu_utilization_percent', 26),
    sample('lockbot_host_memory_used_bytes', 100),
    sample('lockbot_host_memory_total_bytes', 1000),
    sample('lockbot_host_filesystem_used_bytes', 50, { filesystem: 'system' }),
    sample('lockbot_host_filesystem_total_bytes', 100, { filesystem: 'system' }),
    sample('lockbot_accelerator_process_info', 1, {
      device: '1',
      pid: '101',
      container_name: 'test-container-a',
      user: 'developer-a',
      command: 'python task.py [arguments omitted]',
    }),
    sample('lockbot_accelerator_process_info', 1, {
      device: '1',
      pid: '102',
      container_name: 'test-container-b',
      user: 'developer-b',
      command: 'python task.py [arguments omitted]',
    }),
    sample('lockbot_accelerator_process_info_truncated', 1, { device: '1' }),
    sample('lockbot_accelerator_utilization_percent', 99, {
      node: 'unrequested-node',
      device: '0',
    }),
  ]
}
const fetcher = (rows) =>
  vi.fn(
    async () =>
      new Response(
        JSON.stringify({ status: 'success', data: { resultType: 'vector', result: rows } }),
        { headers: { 'content-type': 'application/json' } }
      )
  )
describe('Lockbot unified Prometheus telemetry', () => {
  it('uses one bounded batch and source observation time for every card and host', async () => {
    const fetchImpl = fetcher(payload())
    const result = await collectPrometheus(config(), ['clusterNode'], { fetchImpl, now: () => now })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(result.cards).toHaveLength(2)
    const card = resolveCards(result, config(), 'clusterNode', 'clusterNode:dev1')[0]
    expect(card.device).toBe('1')
    expect(card.metrics.compute.value).toBe(92)
    expect(card.metrics.compute.observedAt).toBe(now - 300000)
    expect(metricStatus(card.metrics.compute, now, 660)).toBe('ok')
    expect(card.processes.map((p) => p.containerName)).toEqual([
      'test-container-a',
      'test-container-b',
    ])
    expect(card.processesTruncated).toBe(true)
    const host = resolveHost(result, config(), 'clusterNode')
    expect(host.cpu.value).toBe(26)
    expect(host.filesystems[0].name).toBe('system')
    expect(host.used.observedAt).toBe(now - 300000)
    const request = fetchImpl.mock.calls[0][1]
    expect(request.credentials).toBe('omit')
    expect(request.headers.Authorization).toBeUndefined()
    expect(request.body.get('query')).toContain('__name__=~')
    expect(request.body.get('query')).toContain('host-a')
    expect(request.body.get('query')).not.toContain('unrequested-node')
  })
  it('does not mistake scrape time for new source samples or collector failures for idle', async () => {
    const result = await collectPrometheus(config(), ['clusterNode'], {
      fetchImpl: fetcher(payload({ age: 900 })),
      now: () => now,
    })
    expect(metricStatus(result.cards[0].metrics.compute, now, 660)).toBe('stale')
    const failed = await collectPrometheus(config(), ['clusterNode'], {
      fetchImpl: fetcher(payload({ success: 0 })),
      now: () => now,
    })
    expect(failed.cards[0].metrics.compute.value).toBe(0)
    expect(metricStatus(failed.cards[0].metrics.compute, now, 660)).toBe('failed')
    expect(metricStatus(failed.hosts[0].cpu, now, 660)).toBe('failed')
  })
  it('rejects duplicate values and invalid host memory without hiding healthy card metrics', async () => {
    const rows = payload().filter((row) => row.metric.__name__ !== 'lockbot_host_memory_used_bytes')
    rows.push(
      sample('lockbot_host_memory_used_bytes', 1001),
      sample('lockbot_accelerator_utilization_percent', 10, { device: '0' })
    )
    const result = await collectPrometheus(config(), ['clusterNode'], {
      fetchImpl: fetcher(rows),
      now: () => now,
    })
    expect(metricStatus(result.hosts[0].used, now, 660)).toBe('invalid')
    expect(metricStatus(result.cards[0].metrics.compute, now, 660)).toBe('invalid')
    expect(metricStatus(result.cards[1].metrics.compute, now, 660)).toBe('ok')
  })
  it('keeps missing source time unknown rather than using evaluation time', async () => {
    const rows = payload().filter(
      (row) => row.metric.__name__ !== 'lockbot_sample_timestamp_seconds'
    )
    const result = await collectPrometheus(config(), ['clusterNode'], {
      fetchImpl: fetcher(rows),
      now: () => now,
    })
    expect(metricStatus(result.cards[0].metrics.compute, now, 660)).toBe('missing')
  })
})
