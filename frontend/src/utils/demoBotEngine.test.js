import { describe, it, expect } from 'vitest'
import { executeCommand } from './demoBotEngine.js'

const NOW = Math.floor(Date.now() / 1000)

describe('deviceUsageText merging', () => {
  it('merges consecutive idle devices with same model', () => {
    const state = {
      node0: [
        { dev_id: 0, dev_model: 'a100', status: 'idle', current_users: [] },
        { dev_id: 1, dev_model: 'a100', status: 'idle', current_users: [] },
        { dev_id: 2, dev_model: 'a100', status: 'idle', current_users: [] },
        { dev_id: 3, dev_model: 'a100', status: 'idle', current_users: [] },
      ],
    }
    const config = {
      CLUSTER_CONFIGS: { node0: ['a100', 'a100', 'a100', 'a100'] },
      _nodeOrder: ['node0'],
    }
    const result = executeCommand(state, 'alice', '', 'DEVICE', config, 'zh')
    // Should show dev0-3 merged, not 4 separate lines
    expect(result).toContain('dev0-3')
    expect(result).not.toContain('dev1')
  })

  it('does not show model when all same', () => {
    const state = {
      node0: [
        { dev_id: 0, dev_model: 'a100', status: 'idle', current_users: [] },
        { dev_id: 1, dev_model: 'a100', status: 'idle', current_users: [] },
      ],
    }
    const config = { CLUSTER_CONFIGS: { node0: ['a100', 'a100'] }, _nodeOrder: ['node0'] }
    const result = executeCommand(state, 'alice', '', 'DEVICE', config, 'zh')
    expect(result).not.toContain('a100')
  })

  it('shows model when heterogeneous', () => {
    const state = {
      node0: [
        { dev_id: 0, dev_model: 'a100', status: 'idle', current_users: [] },
        { dev_id: 1, dev_model: 'h100', status: 'idle', current_users: [] },
      ],
    }
    const config = { CLUSTER_CONFIGS: { node0: ['a100', 'h100'] }, _nodeOrder: ['node0'] }
    const result = executeCommand(state, 'alice', '', 'DEVICE', config, 'zh')
    expect(result).toContain('a100')
    expect(result).toContain('h100')
  })

  it('merges consecutive locked devices with same user/status', () => {
    const state = {
      node0: [
        {
          dev_id: 0,
          dev_model: 'a100',
          status: 'exclusive',
          current_users: [{ user_id: 'bob', start_time: NOW - 3600, duration: 7200 }],
        },
        {
          dev_id: 1,
          dev_model: 'a100',
          status: 'exclusive',
          current_users: [{ user_id: 'bob', start_time: NOW - 3600, duration: 7200 }],
        },
        { dev_id: 2, dev_model: 'a100', status: 'idle', current_users: [] },
      ],
    }
    const config = { CLUSTER_CONFIGS: { node0: ['a100', 'a100', 'a100'] }, _nodeOrder: ['node0'] }
    const result = executeCommand(state, 'alice', '', 'DEVICE', config, 'zh')
    expect(result).toContain('dev0-1')
  })
})
