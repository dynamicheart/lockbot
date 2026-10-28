import { describe, it, expect } from 'vitest'
import { executeCommand } from './demoBotEngine'
import { validateBotState } from './stateValidation'

const configs = { 'node-1': ['GPU', 'GPU'] }
function setup() {
  const state = validateBotState({}, 'QUEUE_DEVICE', configs).state
  return {
    state,
    run: (user, command) =>
      executeCommand(state, user, command, 'QUEUE_DEVICE', { CLUSTER_CONFIGS: configs }),
  }
}
describe('QUEUE_DEVICE demo and state editing', () => {
  it('keeps each card queue independent and requires an explicit claim', () => {
    const { state, run } = setup()
    run('alice', 'lock node-1 dev0 1h')
    run('bob', 'book node-1 dev0 30m')
    run('carol', 'lock node-1 dev1 1h')
    expect(state['node-1'][0].current_users[0].user_id).toBe('alice')
    expect(state['node-1'][0].booking_list[0].user_id).toBe('bob')
    expect(state['node-1'][1].current_users[0].user_id).toBe('carol')
    run('bob', 'lock node-1 dev0')
    expect(state['node-1'][0].current_users[0].user_id).toBe('alice')
    run('alice', 'free node-1 dev0')
    run('bob', 'lock node-1 dev0')
    expect(state['node-1'][0].current_users[0].duration).toBe(1800)
    expect(state['node-1'][1].current_users[0].user_id).toBe('carol')
    expect(run('bob', 'query')).toContain('node-1 dev0')
    expect(run('bob', 'help')).toContain('book node-1 dev0')
  })
  it('rejects invalid card selectors atomically instead of selecting all cards', () => {
    const { state, run } = setup()
    const before = JSON.stringify(state)
    for (const command of [
      'lock node-1 dev9 1h',
      'book node-1 dev2-1 1h',
      'lock node-1 dev-1 1h',
      'slock node-1 dev0 1h',
    ]) {
      run('alice', command)
      expect(JSON.stringify(state)).toBe(before)
    }
  })
  it('normalizes malformed booking lists without discarding valid queues', () => {
    const { state } = setup()
    state['node-1'][0].booking_list = [{ user_id: 'alice', start_time: 1, duration: 600 }]
    state['node-1'][1].booking_list = 'invalid'
    const result = validateBotState(state, 'QUEUE_DEVICE', configs)
    expect(result.state['node-1'][0].booking_list[0].user_id).toBe('alice')
    expect(result.state['node-1'][1].booking_list).toEqual([])
    expect(state['node-1'][1].booking_list).toBe('invalid')
  })
})
