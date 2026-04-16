/**
 * Bot state validation — shared between the mock API and the real frontend.
 *
 * Mirrors the Python _validate_and_align_state logic in router.py.
 * Returns { state, warnings } where state is the normalized copy
 * and warnings lists human-readable issues that were auto-fixed.
 *
 * @param {Function} [t] - Optional i18n translate function for localized warnings.
 */

const VALID_STATUSES = new Set(['idle', 'exclusive', 'shared'])
const REQUIRED_USER_KEYS = ['user_id', 'start_time', 'duration']

function _w(key, params, t) {
  return t ? t(key, params) : `${key} ${JSON.stringify(params)}`
}

function _validateUserInfo(userInfo, label, t) {
  const warnings = []
  if (typeof userInfo !== 'object' || userInfo === null || Array.isArray(userInfo)) return warnings
  for (const key of REQUIRED_USER_KEYS) {
    if (!(key in userInfo)) {
      userInfo[key] = key === 'user_id' ? '' : 0
      warnings.push(_w('stateValidation.missingKey', { label, key }, t))
    }
  }
  if (!('is_notified' in userInfo)) {
    userInfo.is_notified = false
  }
  return warnings
}

function _validateNodeQueueState(state, clusterConfigs, warnings, t) {
  const list = Array.isArray(clusterConfigs) ? clusterConfigs : Object.keys(clusterConfigs)
  const ccSet = new Set(list)
  const result = {}
  for (const name of list) {
    if (!(name in state)) {
      warnings.push(_w('stateValidation.nodeMissing', { name }, t))
      result[name] = { status: 'idle', current_users: [], booking_list: [] }
      continue
    }
    const node = state[name]
    if (typeof node !== 'object' || node === null || Array.isArray(node)) {
      warnings.push(_w('stateValidation.nodeNotDict', { name }, t))
      result[name] = { status: 'idle', current_users: [], booking_list: [] }
      continue
    }
    let st = node.status || 'idle'
    if (!VALID_STATUSES.has(st)) {
      warnings.push(_w('stateValidation.invalidStatus', { name, status: st }, t))
      st = 'idle'
    }
    let currentUsers = node.current_users || []
    if (!Array.isArray(currentUsers)) {
      warnings.push(_w('stateValidation.currentUsersNotList', { name }, t))
      currentUsers = []
    }
    for (let i = 0; i < currentUsers.length; i++) {
      if (
        typeof currentUsers[i] === 'object' &&
        currentUsers[i] !== null &&
        !Array.isArray(currentUsers[i])
      ) {
        warnings.push(
          ..._validateUserInfo(currentUsers[i], `Node '${name}', current_users[${i}]`, t)
        )
      } else {
        warnings.push(_w('stateValidation.userNotDict', { name, field: `current_users[${i}]` }, t))
        currentUsers[i] = { user_id: '', start_time: 0, duration: 0, is_notified: false }
      }
    }
    let bookingList = node.booking_list || []
    if (!Array.isArray(bookingList)) {
      warnings.push(_w('stateValidation.bookingListNotList', { name }, t))
      bookingList = []
    }
    for (let i = 0; i < bookingList.length; i++) {
      if (
        typeof bookingList[i] === 'object' &&
        bookingList[i] !== null &&
        !Array.isArray(bookingList[i])
      ) {
        warnings.push(..._validateUserInfo(bookingList[i], `Node '${name}', booking_list[${i}]`, t))
      } else {
        warnings.push(_w('stateValidation.userNotDict', { name, field: `booking_list[${i}]` }, t))
        bookingList[i] = { user_id: '', start_time: 0, duration: 0, is_notified: false }
      }
    }
    result[name] = { status: st, current_users: currentUsers, booking_list: bookingList }
  }
  for (const name of Object.keys(state)) {
    if (!ccSet.has(name)) {
      warnings.push(_w('stateValidation.nodeNotInConfig', { name }, t))
    }
  }
  return result
}

function _validateDeviceState(state, clusterConfigs, warnings, t) {
  const ccKeys = new Set(Object.keys(clusterConfigs))
  const result = {}
  for (const [nodeKey, devices] of Object.entries(clusterConfigs)) {
    if (!(nodeKey in state)) {
      warnings.push(_w('stateValidation.nodeMissing', { name: nodeKey }, t))
      result[nodeKey] = devices.map((model, i) => ({
        dev_id: i,
        dev_model: model,
        status: 'idle',
        current_users: [],
      }))
      continue
    }
    let nodeState = state[nodeKey]
    if (!Array.isArray(nodeState)) {
      warnings.push(_w('stateValidation.nodeNotList', { name: nodeKey }, t))
      result[nodeKey] = devices.map((model, i) => ({
        dev_id: i,
        dev_model: model,
        status: 'idle',
        current_users: [],
      }))
      continue
    }
    const expectedCount = devices.length
    if (nodeState.length > expectedCount) {
      warnings.push(
        _w(
          'stateValidation.deviceExcess',
          { name: nodeKey, actual: nodeState.length, expected: expectedCount },
          t
        )
      )
      nodeState = nodeState.slice(0, expectedCount)
    }
    const deviceList = []
    for (let i = 0; i < expectedCount; i++) {
      let dev
      if (i < nodeState.length) {
        dev = nodeState[i]
        if (typeof dev !== 'object' || dev === null || Array.isArray(dev)) {
          warnings.push(_w('stateValidation.deviceNotDict', { name: nodeKey, index: i }, t))
          dev = {}
        }
      } else {
        warnings.push(_w('stateValidation.deviceMissing', { name: nodeKey, index: i }, t))
        dev = {}
      }
      if (!('dev_id' in dev)) dev.dev_id = i
      if (dev.dev_id !== i) {
        warnings.push(
          _w(
            'stateValidation.devIdCorrected',
            { name: nodeKey, index: i, old: dev.dev_id, new: i },
            t
          )
        )
        dev.dev_id = i
      }
      dev.dev_model = devices[i]
      let st = dev.status || 'idle'
      if (!VALID_STATUSES.has(st)) {
        warnings.push(
          _w('stateValidation.invalidStatus', { name: `${nodeKey}, device ${i}`, status: st }, t)
        )
        st = 'idle'
      }
      dev.status = st
      let currentUsers = dev.current_users || []
      if (!Array.isArray(currentUsers)) {
        warnings.push(
          _w('stateValidation.currentUsersNotList', { name: `${nodeKey}, device ${i}` }, t)
        )
        currentUsers = []
      }
      for (let j = 0; j < currentUsers.length; j++) {
        if (
          typeof currentUsers[j] === 'object' &&
          currentUsers[j] !== null &&
          !Array.isArray(currentUsers[j])
        ) {
          warnings.push(
            ..._validateUserInfo(
              currentUsers[j],
              `Node '${nodeKey}', device ${i}, current_users[${j}]`,
              t
            )
          )
        } else {
          warnings.push(
            _w(
              'stateValidation.userNotDict',
              { name: `${nodeKey}, device ${i}`, field: `current_users[${j}]` },
              t
            )
          )
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
      warnings.push(_w('stateValidation.nodeNotInConfig', { name }, t))
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
 * @param {Function} [t] - Optional i18n translate function
 * @returns {{ state: object, warnings: string[] }}
 */
export function validateBotState(state, botType, clusterConfigs, t) {
  if (typeof state !== 'object' || state === null || Array.isArray(state)) {
    return {
      state: _buildDefaultState(clusterConfigs, botType),
      warnings: [_w('stateValidation.stateNotDict', {}, t)],
    }
  }
  // Deep clone to avoid mutating the input
  const cloned = JSON.parse(JSON.stringify(state))
  const warnings = []
  const normalized =
    botType === 'DEVICE'
      ? _validateDeviceState(cloned, clusterConfigs, warnings, t)
      : _validateNodeQueueState(cloned, clusterConfigs, warnings, t)
  return { state: normalized, warnings }
}

function _buildDefaultState(clusterConfigs, botType) {
  if (botType === 'DEVICE') {
    const state = {}
    for (const [nodeKey, devices] of Object.entries(clusterConfigs)) {
      state[nodeKey] = devices.map((model, i) => ({
        dev_id: i,
        dev_model: model,
        status: 'idle',
        current_users: [],
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
