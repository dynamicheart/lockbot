import { describe, expect, it } from 'vitest'
import { executeCommand } from './demoBotEngine'
const now = Math.floor(Date.now() / 1000)
const user = (id, duration = 600) => ({ user_id: id, start_time: now, duration })
const cfg = {
  CLUSTER_CONFIGS: { a: ['gpu', 'gpu'], b: ['gpu'] },
  FORBID_RELOCK: true,
  MAX_RESOURCES_PER_USER: 1,
}

describe('resource fairness preview', () => {
  it.each(['zh', 'en'])('blocks renewal and preserves all targets in %s', (lang) => {
    const state = {
      a: { status: 'exclusive', current_users: [user('alice')], booking_list: [] },
      b: { status: 'idle', current_users: [], booking_list: [] },
    }
    const before = structuredClone(state)
    expect(executeCommand(state, 'alice', 'lock a 10m', 'QUEUE', cfg, lang)).toContain('❌')
    expect(executeCommand(state, 'alice', 'lock b 10m', 'QUEUE', cfg, lang)).toContain('❌')
    expect(state).toEqual(before)
  })
  it('counts cards rather than machines', () => {
    const state = {
      a: [
        { dev_id: 0, status: 'exclusive', current_users: [user('alice')] },
        { dev_id: 1, status: 'idle', current_users: [] },
      ],
    }
    const before = structuredClone(state)
    expect(executeCommand(state, 'alice', 'lock a dev1 10m', 'DEVICE', cfg)).toContain('❌')
    expect(state).toEqual(before)
  })
  it('does not extend a queued claim or take', () => {
    const state = { a: { status: 'idle', current_users: [], booking_list: [user('alice')] } }
    const before = structuredClone(state)
    expect(executeCommand(state, 'alice', 'lock a 20m', 'QUEUE', cfg)).toContain('❌')
    expect(executeCommand(state, 'alice', 'take a 20m', 'QUEUE', cfg)).toContain('❌')
    expect(state).toEqual(before)
    expect(executeCommand(state, 'alice', 'lock a', 'QUEUE', cfg)).not.toContain('❌')
  })
})

it.each(['zh', 'en'])('help does not suggest forbidden renewal in %s', (lang) => {
  const help = executeCommand({}, 'alice', 'help', 'QUEUE', cfg, lang)
  expect(help).not.toContain('repeated lock extends')
  expect(help).not.toContain('重复lock增加时间')
  expect(help).toMatch(/禁止续期|no renewal/)
})
