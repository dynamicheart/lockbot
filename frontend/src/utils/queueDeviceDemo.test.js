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
    expect(run('bob', 'query')).toContain('node-1\n  dev0')
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
  it.each(['zh', 'en'])('groups cards and their queues with optional models in %s', (lang) => {
    const { state, run } = setup()
    run('alice', 'lock node-1 dev0 1h')
    run('bob', 'book node-1 dev0 30m')
    run('carol', 'lock node-1 dev1 1h')
    const before = JSON.stringify(state)
    const options = { CLUSTER_CONFIGS: configs, DISPLAY_DEVICE_MODEL: true }
    const reply = executeCommand(state, 'alice', 'query', 'QUEUE_DEVICE', options, lang)
    expect(reply.match(/node-1/g)).toHaveLength(1)
    const [first, second] = reply.split('  dev1')
    expect(first).toContain('  dev0 GPU alice')
    expect(first).toContain('bob')
    expect(second).toContain('GPU carol')
    expect(second).not.toContain('bob')
    const plain = executeCommand(
      state,
      'alice',
      'query',
      'QUEUE_DEVICE',
      { CLUSTER_CONFIGS: configs },
      lang
    )
    expect(plain).not.toContain('GPU')
    expect(JSON.stringify(state)).toBe(before)
  })
})

describe('QUEUE_DEVICE contiguous display ranges', () => {
  it.each(['zh', 'en'])(
    'merges equal card leases and queues in %s without changing state',
    (lang) => {
      const { state, run } = setup()
      const reply = run('alice', 'lock node-1 dev0-1 1h')
      expect(reply).toContain('dev0-1 alice')
      run('bob', 'book node-1 dev0-1 30m')
      state['node-1'].forEach((card, index) => {
        card.current_users[0].holding_id = `lease-${index}`
        card.booking_list[0].holding_id = `booking-${index}`
        card.current_users[0].source = 'api'
        card.booking_list[0].source = 'api'
      })
      const before = JSON.stringify(state)
      const text = executeCommand(
        state,
        'alice',
        'query',
        'QUEUE_DEVICE',
        {
          CLUSTER_CONFIGS: configs,
          DISPLAY_DEVICE_MODEL: true,
        },
        lang
      )
      expect(text).toContain('node-1\n  dev0-1 GPU alice')
      expect(text.match(/bob/g)).toHaveLength(1)
      expect(JSON.stringify(state)).toBe(before)
    }
  )
  it.each(['lease', 'duration', 'source', 'queue', 'notify', 'queueTime', 'model', 'purpose'])(
    'keeps cards separate for different %s',
    (difference) => {
      const { state, run } = setup()
      run('alice', 'lock node-1 dev0-1 1h')
      run('bob', 'book node-1 dev0-1 30m')
      run('carol', 'book node-1 dev0-1 20m')
      const second = state['node-1'][1]
      if (difference === 'lease') second.current_users[0].start_time -= 1
      if (difference === 'duration') second.current_users[0].duration += 1
      if (difference === 'source') second.current_users[0].source = 'api'
      if (difference === 'queue') second.booking_list.reverse()
      if (difference === 'notify') second.booking_list[0].is_notified = true
      if (difference === 'queueTime') second.booking_list[0].start_time -= 1
      if (difference === 'model') second.dev_model = 'other-model'
      if (difference === 'purpose') second.current_users[0].usage = { purpose: 'other task' }
      const text = run('alice', 'query')
      expect(text).not.toContain('dev0-1')
      expect(text).toContain('  dev0 ')
      expect(text).toContain('  dev1 ')
    }
  )
  it('does not merge across missing cards or machines', () => {
    const { state, run } = setup()
    state['node-1'][1].dev_id = 2
    const text = run('alice', 'query')
    expect(text).not.toContain('dev0-2')
    expect(text).toContain('  dev0 ')
    expect(text).toContain('  dev2 ')
  })
})
