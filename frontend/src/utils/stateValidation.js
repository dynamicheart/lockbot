/**
 * Bot state validation — shared between the mock API and the real frontend.
 *
 * Mirrors the Python _validate_and_align_state logic in router.py.
 * Returns { state, warnings } where state is the normalized copy
 * and warnings lists human-readable issues that were auto-fixed.
 */

const VALID_STATUSES = new Set(['idle', 'exclusive', 'shared'])
const REQUIRED_USER_KEYS = ['user_id', 'start_time', 'duration']

function _validateUserInfo(userInfo, label) {
  const warnings = []
  if (typeof userInfo !== 'object' || userInfo === null || Array.isArray(userInfo)) return warnings
  for (const key of REQUIRED_USER_KEYS) {
    if (!(key in userInfo)) {
      userInfo[key] = key === 'user_id' ? '' : 0
      warnings.push(`${label}: missing '${key}', set to default`)
    }
  }
  if (!('is_notified' in userInfo)) {
    userInfo.is_notified = false
  }
  return warnings
}

function _validateNodeQueueState(state, clusterConfigs, warnings) {
  const list = Array.isArray(clusterConfigs) ? clusterConfigs : Object.keys(clusterConfigs)
  const ccSet = new Set(list)
  const result = {}
  for (const name of list) {
    if (!(name in state)) {
      warnings.push(`Node '${name}' missing from state, added with defaults`)
      result[name] = { status: 'idle', current_users: [], booking_list: [] }
      continue
    }
    const node = state[name]
    if (typeof node !== 'object' || node === null || Array.isArray(node)) {
      warnings.push(`Node '${name}' is not a dict, replaced with defaults`)
      result[name] = { status: 'idle', current_users: [], booking_list: [] }
      continue
    }
    let st = node.status || 'idle'
    if (!VALID_STATUSES.has(st)) {
      warnings.push(`Node '${name}': invalid status '${st}', reset to 'idle'`)
      st = 'idle'
    }
    let currentUsers = node.current_users || []
    if (!Array.isArray(currentUsers)) {
      warnings.push(`Node '${name}': current_users is not a list, reset to []`)
      currentUsers = []
    }
    for (let i = 0; i < currentUsers.length; i++) {
      if (typeof currentUsers[i] === 'object' && currentUsers[i] !== null && !Array.isArray(currentUsers[i])) {
        warnings.push(..._validateUserInfo(currentUsers[i], `Node '${name}', current_users[${i}]`))
      } else {
        warnings.push(`Node '${name}', current_users[${i}]: not a dict, removed`)
        currentUsers[i] = { user_id: '', start_time: 0, duration: 0, is_notified: false }
      }
    }
    let bookingList = node.booking_list || []
    if (!Array.isArray(bookingList)) {
      warnings.push(`Node '${name}': booking_list is not a list, reset to []`)
      bookingList = []
    }
    for (let i = 0; i < bookingList.length; i++) {
      if (typeof bookingList[i] === 'object' && bookingList[i] !== null && !Array.isArray(bookingList[i])) {
        warnings.push(..._validateUserInfo(bookingList[i], `Node '${name}', booking_list[${i}]`))
      } else {
        warnings.push(`Node '${name}', booking_list[${i}]: not a dict, removed`)
        bookingList[i] = { user_id: '', start_time: 0, duration: 0, is_notified: false }
      }
    }
    result[name] = { status: st, current_users: currentUsers, booking_list: bookingList }
  }
  for (const name of Object.keys(state)) {
    if (!ccSet.has(name)) {
      warnings.push(`Node '${name}' not in cluster_configs, removed`)
    }
  }
  return result
}

function _validateDeviceState(state, clusterConfigs, warnings) {
  const ccKeys = new Set(Object.keys(clusterConfigs))
  const result = {}
  for (const [nodeKey, devices] of Object.entries(clusterConfigs)) {
    if (!(nodeKey in state)) {
      warnings.push(`Node '${nodeKey}' missing from state, added with defaults`)
      result[nodeKey] = devices.map((model, i) => ({
        dev_id: i, dev_model: model, status: 'idle', current_users: [],
      }))
      continue
    }
    let nodeState = state[nodeKey]
    if (!Array.isArray(nodeState)) {
      warnings.push(`Node '${nodeKey}' is not a list, replaced with defaults`)
      result[nodeKey] = devices.map((model, i) => ({
        dev_id: i, dev_model: model, status: 'idle', current_users: [],
      }))
      continue
    }
    const expectedCount = devices.length
    if (nodeState.length > expectedCount) {
      warnings.push(`Node '${nodeKey}': has ${nodeState.length} devices, expected ${expectedCount}, excess removed`)
      nodeState = nodeState.slice(0, expectedCount)
    }
    const deviceList = []
    for (let i = 0; i < expectedCount; i++) {
      let dev
      if (i < nodeState.length) {
        dev = nodeState[i]
        if (typeof dev !== 'object' || dev === null || Array.isArray(dev)) {
          warnings.push(`Node '${nodeKey}', device ${i}: not a dict, replaced with default`)
          dev = {}
        }
      } else {
        warnings.push(`Node '${nodeKey}', device ${i}: missing, added with default`)
        dev = {}
      }
      if (!('dev_id' in dev)) dev.dev_id = i
      if (dev.dev_id !== i) {
        warnings.push(`Node '${nodeKey}', device ${i}: dev_id ${dev.dev_id} corrected to ${i}`)
        dev.dev_id = i
      }
      dev.dev_model = devices[i]
      let st = dev.status || 'idle'
      if (!VALID_STATUSES.has(st)) {
        warnings.push(`Node '${nodeKey}', device ${i}: invalid status '${st}', reset to 'idle'`)
        st = 'idle'
      }
      dev.status = st
      let currentUsers = dev.current_users || []
      if (!Array.isArray(currentUsers)) {
        warnings.push(`Node '${nodeKey}', device ${i}: current_users is not a list, reset to []`)
        currentUsers = []
      }
      for (let j = 0; j < currentUsers.length; j++) {
        if (typeof currentUsers[j] === 'object' && currentUsers[j] !== null && !Array.isArray(currentUsers[j])) {
          warnings.push(..._validateUserInfo(currentUsers[j], `Node '${nodeKey}', device ${i}, current_users[${j}]`))
        } else {
          warnings.push(`Node '${nodeKey}', device ${i}, current_users[${j}]: not a dict, removed`)
          currentUsers[j] = { user_id: '', start_time: 0, duration: 0, is_notified: false }
        }
      }
      dev.current_users = currentUsers
      deviceList.push(dev)
    }
    result[nodeKey] = deviceList
  }
  for (const name of Object.keys(state)) {
    if (!ccKeys.has(name)) {
      warnings.push(`Node '${name}' not in cluster_configs, removed`)
    }
  }
  return result
}

/**
 * Validate and normalize bot state against cluster_configs.
 *
 * @param {object} state - The state object to validate (will NOT be mutated)
 * @param {string} botType - 'NODE', 'DEVICE', or 'QUEUE'
 * @param {object|Array} clusterConfigs - The bot's cluster_configs
 * @returns {{ state: object, warnings: string[] }}
 */
export function validateBotState(state, botType, clusterConfigs) {
  if (typeof state !== 'object' || state === null || Array.isArray(state)) {
    return { state: _buildDefaultState(clusterConfigs, botType), warnings: ['State is not a dict, replaced with defaults'] }
  }
  // Deep clone to avoid mutating the input
  const cloned = JSON.parse(JSON.stringify(state))
  const warnings = []
  const normalized = botType === 'DEVICE'
    ? _validateDeviceState(cloned, clusterConfigs, warnings)
    : _validateNodeQueueState(cloned, clusterConfigs, warnings)
  return { state: normalized, warnings }
}

function _buildDefaultState(clusterConfigs, botType) {
  if (botType === 'DEVICE') {
    const state = {}
    for (const [nodeKey, devices] of Object.entries(clusterConfigs)) {
      state[nodeKey] = devices.map((model, i) => ({
        dev_id: i, dev_model: model, status: 'idle', current_users: [],
      }))
    }
    return state
  }
  const state = {}
  const list = Array.isArray(clusterConfigs) ? clusterConfigs : Object.keys(clusterConfigs)
  for (const name of list) {
    state[name] = { status: 'idle', current_users: [], booking_list: [] }
  }
  return state
}
