/**
 * demoBotEngine.js  --  Pure-function reimplementation of the Python bot commands.
 *
 * This module provides the command-parsing and state-mutating logic used by
 * the real LockBot back-end (node_bot.py, device_bot.py, queue_bot.py) so
 * that the demo mode can execute commands client-side and show realistic
 * replies without a Python process.
 *
 * Output format, emojis, and messages match the Python i18n/en.py and zh.py
 * strings exactly.  The `lang` parameter selects the locale ('en' or 'zh').
 */

// ---------------------------------------------------------------------------
// i18n message catalogue  (mirrors Python lockbot/core/i18n/en.py + zh.py)
// ---------------------------------------------------------------------------

const _MSGS = {
  en: {
    // Duration
    'duration.days': '{value} days',
    'duration.hours': '{value} hours',
    'duration.minutes': '{value} minutes',
    // Access mode
    'access_mode.shared': '(shared)',
    'access_mode.exclusive': '(exclusive)',
    // Status
    'status.idle': 'idle',
    // Success
    'success.resource_locked': '✅ Resource locked successfully\n\n',
    'success.resource_released': '✅ Resource released successfully\n\n',
    'success.resource_force_released': '✅ Resource force-released by {user_id}\n\n',
    // Labels
    'label.before_release': 'Before release:\n',
    'label.after_release': 'After release:\n',
    'label.before_take': 'Before take:\n',
    'label.after_take': 'After take:\n',
    'label.queue_list': '⌛️ Queue:\n',
    'label.queue_item': '  {index}. {user_id} {duration} est. wait {wait_time}\n',
    // Errors
    'error.invalid_command_format': '❌ Invalid command format: {command}',
    'error.invalid_node_key': "Invalid node '{node_key}'\n\nPlease choose from: {valid_keys}\n",
    'error.node_in_use_or_shared': 'Node is in use by others or in shared mode\n\n',
    'error.node_exclusive_mode': 'Node is in exclusive mode\n\n',
    'error.lock_max_duration_exceeded': 'Note: consecutive lock cannot exceed {max_duration}\n\n',
    'error.slock_max_duration_exceeded': 'Note: consecutive slock cannot exceed {max_duration}\n\n',
    'error.node_not_requested': 'You have not requested this node\n',
    'error.unrecognized_command': '❌ Unrecognized command: {command}',
    // Device errors
    'error.device_in_use_or_shared': 'Device is in exclusive use or in shared mode\n\n',
    'error.device_exclusive_mode': 'Device is in exclusive use\n\n',
    'error.device_not_requested': 'You have not requested this device\n',
    // Queue errors
    'error.node_in_use_or_not_your_turn': 'Node is in use by others, or it is not your turn yet',
    'error.already_locked': 'You are already using or have already booked',
    'error.locked_user_cannot_take': 'You are already using this node',
    'error.slock_not_supported': 'QueueBot does not support slock',
    // Queue success
    'success.booking_added': '🗓️ Booking added successfully\n\n',
    'success.take_success_by': '🏁 Resource taken by {user_id}\n\n',
    'success.kicklock_cleared': '✅ Lock cleared by {user_id}\n\n',
    // Notify
    'notify.wait_time_increased': 'Note: wait times have been updated\n\n',
    // Query
    'query.cluster_usage_title': 'ℹ️ Cluster Usage Details\n\n',
    // Device usage
    'device_usage.node_header': '{node_key} usage:\n',
    // Help
    'help.title': '📖 Usage Guide\n',
    'help.section1_title': '1. Request resource\n',
    'help.rule1_default_duration':
      '    Rule 1: Default duration {default_duration}, repeated lock extends time, d(day),h(hour),m(min)\n',
    'help.rule2_post_expiry_notification':
      '    Rule 2: A reminder will be sent after resource expires\n',
    'help.rule3_lock_modes':
      '    Rule 3: lock for exclusive, slock for shared (multiple users can slock)\n',
    'help.section2_title': '2. Release resource (unlock and free are interchangeable)\n',
    'help.free_all': '    free (release all your resources)\n',
    'help.section3_title': "3. Force release others' resource (will notify affected users)\n",
    'help.section4_title': '4. Help: help or h\n',
    'help.section5_title': '5. Query:\n',
    'help.query_at_bot': '    Just @ the bot\n',
    'help.max_duration_warning': 'Note: consecutive lock/slock cannot exceed {max_duration}\n\n',
    'help.bot_version': 'Bot version: {version}\n',
    'help.bot_id': 'Bot ID: {bot_id}\n',
    'help.bot_owner': 'Owner: {owner}\n',
    // Help NODE examples
    'help.lock_example': '    lock {node} (lock node {node})\n',
    'help.lock_duration_example': '    lock {node} {duration} (lock node {node} for {duration})\n',
    'help.slock_example': '    slock {node} (shared-lock node {node})\n',
    'help.unlock_example': '    unlock {node} (release node {node})\n',
    'help.kickout_example': '    kickout {node} (force-release node {node})\n',
    'help.query_node_example': '    query {node} (query node {node})\n',
    // Help DEVICE examples
    'help.lock_all_devices_example': '    lock {node} (lock all devices on node)\n',
    'help.lock_device_example': '    lock {node} 0 (lock device 0 on {node})\n',
    'help.lock_device_duration_example': '    lock {node} 0 2h (lock device 0 on {node} for 2h)\n',
    'help.lock_device_range_example': '    lock {node} 0-3 (lock devices 0-3 on {node})\n',
    'help.slock_device_range_example': '    slock {node} 0-3 (shared-lock devices 0-3 on {node})\n',
    'help.unlock_device_example': '    unlock {node} (release all your devices on node)\n',
    'help.unlock_device_range_example': '    unlock {node} 0-3 (release devices 0-3 on {node})\n',
    'help.free_device_range_example': '    free {node} 0-3 (release devices 0-3 on {node})\n',
    'help.free_node_all_example': '    free {node} (release all your devices on {node})\n',
    'help.kickout_device_example': '    kickout {node} (force-release all devices on node)\n',
    'help.kickout_device_range_example':
      '    kickout {node} 0-3 (force-release devices 0-3 on {node})\n',
    'help.kickout_device_range2_example':
      '    kickout {node} 0 (force-release device 0 on {node})\n',
    'help.resource_list_title': 'Resource list:\n',
    'help.resource_list_item': '    {node_key}: dev_id 0~{max_dev}\n',
    // Help QUEUE extras
    'help.rule3_lock_exclusive': '    Rule 3: lock for exclusive access\n',
    'help.section_booking_title': '2. Book\n',
    'help.book_example': '    book {node} (book node {node})\n',
    'help.book_duration_example': '    book {node} {duration} (book node {node} for {duration})\n',
    'help.section_take_title': '3. Take (preempt)\n',
    'help.take_example': '    take {node} (take node {node})\n',
    'help.section_release_title': '4. Release resource (unlock and free are interchangeable)\n',
    'help.section_kickout_title':
      "5. Force release others' resource (will notify affected users)\n",
    'help.section_cancel_booking_title': '5. Cancel booking\n',
    'help.section_kicklock_title': '7. Force release lock (booking info preserved)\n',
    'help.section_help_title_queue': '6. Help: help or h\n',
    'help.section_query_title_queue': '7. Query:\n',
  },
  zh: {
    // Duration
    'duration.days': '{value} 天',
    'duration.hours': '{value} 小时',
    'duration.minutes': '{value} 分钟',
    // Access mode
    'access_mode.shared': '(共享)',
    'access_mode.exclusive': '(独占)',
    // Status
    'status.idle': '空闲',
    // Success
    'success.resource_locked': '✅【资源申请成功】\n\n',
    'success.resource_released': '✅【资源释放成功】\n\n',
    'success.resource_force_released': '✅【资源强制释放成功】by {user_id}\n\n',
    // Labels
    'label.before_release': '【释放前】：\n',
    'label.after_release': '【释放后】：\n',
    'label.before_take': '【抢占前】：\n',
    'label.after_take': '【抢占后】：\n',
    'label.queue_list': '⌛️ 排队:\n',
    'label.queue_item': '  {index}. {user_id} {duration} 预计等待 {wait_time}\n',
    // Errors
    'error.invalid_command_format': '❌【命令格式有误】{command}',
    'error.invalid_node_key': '【节点{node_key}有误】\n\n节点应在{valid_keys}里面选择\n',
    'error.node_in_use_or_shared': '【节点正在被他人使用或处于共享状态】\n\n',
    'error.node_exclusive_mode': '【节点正处于独占状态】\n\n',
    'error.lock_max_duration_exceeded': '【注意: 目前禁止连续lock超过{max_duration}】\n\n',
    'error.slock_max_duration_exceeded': '【注意: 目前禁止连续slock超过{max_duration}】\n\n',
    'error.node_not_requested': '【你并未申请过该节点资源】\n',
    'error.unrecognized_command': '❌【未识别的命令】{command}',
    // Device errors
    'error.device_in_use_or_shared': '【设备正在被他人独占使用或处于共享状态】\n\n',
    'error.device_exclusive_mode': '【设备正在被独占使用】\n\n',
    'error.device_not_requested': '【你并未申请过该设备资源】\n',
    // Queue errors
    'error.node_in_use_or_not_your_turn': '节点正在被他人使用，或未到排队顺序',
    'error.already_locked': '你已经正在使用或者已经排过队',
    'error.locked_user_cannot_take': '你已经正在使用',
    'error.slock_not_supported': 'QueueBot不支持slock',
    // Queue success
    'success.booking_added': '🗓️【排队成功】\n\n',
    'success.take_success_by': '🏁【资源抢占成功】by {user_id}\n\n',
    'success.kicklock_cleared': '✅【锁定已清空】by {user_id}\n\n',
    // Notify
    'notify.wait_time_increased': '请注意等待时间已增加 \n\n',
    // Query
    'query.cluster_usage_title': 'ℹ️【集群使用详情】\n\n',
    // Device usage
    'device_usage.node_header': '{node_key}使用情况:\n',
    // Help
    'help.title': '📖【使用方法】\n',
    'help.section1_title': '1. 申请资源\n',
    'help.rule1_default_duration':
      '    规则1: 默认时间{default_duration}, 重复lock增加时间, d(天),h(时),m(分)\n',
    'help.rule2_post_expiry_notification': '    规则2: 资源时间用时耗尽后,会进行提醒\n',
    'help.rule3_lock_modes': '    规则3: lock申请独占资源, slock申请共享资源(可多人同时slock)\n',
    'help.section2_title': '2. 释放资源 (unlock和free通用)\n',
    'help.free_all': '    free (释放自己申请的所有资源)\n',
    'help.section3_title': '3. 强制释放他人资源 (会at相关人员)\n',
    'help.section4_title': '4. 帮助: help或者h\n',
    'help.section5_title': '5. 查询:\n',
    'help.query_at_bot': '    直接at机器人\n',
    'help.max_duration_warning': '【注意: 目前禁止连续lock/slock超过{max_duration}】\n\n',
    'help.bot_version': '机器人版本: {version}\n',
    'help.bot_id': '机器人ID: {bot_id}\n',
    'help.bot_owner': '管理人: {owner}\n',
    // Help NODE examples
    'help.lock_example': '    lock {node} (锁定{node}节点)\n',
    'help.lock_duration_example': '    lock {node} {duration} (锁定{node}节点{duration})\n',
    'help.slock_example': '    slock {node} (共享锁定{node}节点)\n',
    'help.unlock_example': '    unlock {node} (释放{node}节点)\n',
    'help.kickout_example': '    kickout {node} (强制释放{node}节点)\n',
    'help.query_node_example': '    query {node} (查询{node}节点)\n',
    // Help DEVICE examples
    'help.lock_all_devices_example': '    lock {node} (锁定当前节点的所有设备)\n',
    'help.lock_device_example': '    lock {node} 0 (锁定{node}节点的0号设备)\n',
    'help.lock_device_duration_example': '    lock {node} 0 2h (锁定{node}节点的0号设备2小时)\n',
    'help.lock_device_range_example': '    lock {node} 0-3 (锁定{node}节点的0-3号设备)\n',
    'help.slock_device_range_example': '    slock {node} 0-3 (共享锁定{node}节点的0-3号设备)\n',
    'help.unlock_device_example': '    unlock {node} (释放当前节点所有申请过的设备)\n',
    'help.unlock_device_range_example': '    unlock {node} 0-3 (释放{node}节点的0-3号设备)\n',
    'help.free_device_range_example': '    free {node} 0-3 (释放{node}节点的0-3号设备)\n',
    'help.free_node_all_example': '    free {node} (释放{node}节点所有申请过的设备)\n',
    'help.kickout_device_example': '    kickout {node} (强制释放当前节点的所有设备)\n',
    'help.kickout_device_range_example': '    kickout {node} 0-3 (强制释放{node}节点的0-3号设备)\n',
    'help.kickout_device_range2_example': '    kickout {node} 0 (强制释放{node}节点的0号设备)\n',
    'help.resource_list_title': '资源列表:\n',
    'help.resource_list_item': '    {node_key}: dev_id 0~{max_dev}\n',
    // Help QUEUE extras
    'help.rule3_lock_exclusive': '    规则3: lock申请独占资源\n',
    'help.section_booking_title': '2. 排队\n',
    'help.book_example': '    book {node} (排队等候{node}节点)\n',
    'help.book_duration_example': '    book {node} {duration} (排队等候{node}节点{duration})\n',
    'help.section_take_title': '3. 抢占\n',
    'help.take_example': '    take {node} (抢占{node}节点)\n',
    'help.section_release_title': '4. 释放资源 (unlock和free通用)\n',
    'help.section_kickout_title': '5. 强制释放他人资源 (会at相关人员)\n',
    'help.section_cancel_booking_title': '5. 取消排队\n',
    'help.section_kicklock_title': '7. 强制释放锁(排队信息不清除)\n',
    'help.section_help_title_queue': '6. 帮助: help或者h\n',
    'help.section_query_title_queue': '7. 查询:\n',
  },
}

/**
 * Translate a message key, substituting {param} placeholders.
 */
function _t(lang, key, params = {}) {
  let msg = (_MSGS[lang] || _MSGS.en)[key] || _MSGS.en[key] || key
  for (const [k, v] of Object.entries(params)) {
    msg = msg.replace(new RegExp(`\\{${k}\\}`, 'g'), v)
  }
  return msg
}

// ---------------------------------------------------------------------------
// Duration helpers
// ---------------------------------------------------------------------------

/**
 * Parse a duration string like "3d", "2h", "30m", "1.5h" into seconds.
 * Returns -1 on parse failure.
 */
export function durationToSeconds(str) {
  const m = str.match(/^([0-9]+\.?[0-9]*)([dhm])$/i)
  if (!m) return -1
  const val = parseFloat(m[1])
  const unit = m[2].toLowerCase()
  switch (unit) {
    case 'd':
      return Math.round(val * 86400)
    case 'h':
      return Math.round(val * 3600)
    case 'm':
      return Math.round(val * 60)
    default:
      return -1
  }
}

/**
 * Format seconds into a human-readable string.
 * Matches Python format_duration: "1.0 days", "2.5 hours", "30 minutes".
 */
export function formatDuration(seconds, lang = 'en') {
  if (seconds >= 86400) {
    return _t(lang, 'duration.days', { value: (seconds / 86400).toFixed(1) })
  } else if (seconds >= 3600) {
    return _t(lang, 'duration.hours', { value: (seconds / 3600).toFixed(1) })
  } else {
    return _t(lang, 'duration.minutes', { value: Math.round(seconds / 60) })
  }
}

/** Remaining duration given a start timestamp and total duration. Clamped to 0. */
function remainingDuration(startTime, duration) {
  return Math.max(duration - (Math.floor(Date.now() / 1000) - startTime), 0)
}

// ---------------------------------------------------------------------------
// User-info helpers  (mirrors lockbot/core/utils.py)
// ---------------------------------------------------------------------------

function createUser(userId, duration, startTime) {
  return { user_id: userId, start_time: startTime, duration, is_notified: false }
}

function findUser(users, userId) {
  return users.find((u) => u.user_id === userId) || null
}

function removeUser(users, userId) {
  const idx = users.findIndex((u) => u.user_id === userId)
  if (idx >= 0) users.splice(idx, 1)
}

// ---------------------------------------------------------------------------
// Regex building blocks
// ---------------------------------------------------------------------------

const DURATION_RE = /([0-9]+\.?[0-9]*)([dhm])\s*$/

// ---------------------------------------------------------------------------
// Parse helpers
// ---------------------------------------------------------------------------

function splitNodeList(raw) {
  return raw
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

function parseDurationTail(command) {
  const m = command.match(DURATION_RE)
  if (!m) return null
  return {
    seconds: durationToSeconds(`${m[1]}${m[2]}`),
    rest: command.slice(0, command.lastIndexOf(m[0])).trim(),
  }
}

function parseDevSpec(rest, clusterConfigs, nodeKey) {
  const m = rest.match(new RegExp(`dev\\s*(.+)`, 'i'))
  if (!m) return null
  const devStr = m[1].trim()
  let devIds
  if (devStr.includes('-')) {
    const [lo, hi] = devStr.split('-').map(Number)
    if (lo > hi) return null
    devIds = Array.from({ length: hi - lo + 1 }, (_, i) => lo + i)
  } else {
    devIds = [...new Set(devStr.split(/[,，、]/).map(Number))]
  }
  const numDevs = Array.isArray(clusterConfigs[nodeKey]) ? clusterConfigs[nodeKey].length : 0
  if (devIds.some((d) => d < 0 || d >= numDevs)) return null
  return devIds
}

// ---------------------------------------------------------------------------
// Format helpers (match Python i18n exactly)
// ---------------------------------------------------------------------------

function formatAccessMode(status, lang = 'en') {
  if (status === 'exclusive') return _t(lang, 'access_mode.exclusive')
  if (status === 'shared') return _t(lang, 'access_mode.shared')
  return ''
}

// ---------------------------------------------------------------------------
// Usage display  (mirrors _current_usage in each Python bot)
// ---------------------------------------------------------------------------

/**
 * Build NODE usage text. Matches Python NodeBot._current_usage.
 */
function nodeUsageText(state, nodeFilter, lang) {
  let text = ''
  for (const [key, node] of Object.entries(state)) {
    if (nodeFilter && key !== nodeFilter) continue
    if (node.status === 'idle') {
      text += `${key} ${_t(lang, 'status.idle')}\n`
    } else {
      node.current_users.forEach((u, idx) => {
        const prefix = idx === 0 ? key : ''
        const rem = remainingDuration(u.start_time, u.duration)
        const uid = u.user_id + formatAccessMode(node.status, lang)
        text += `${prefix} ${uid}  ${formatDuration(rem, lang)}\n`
      })
    }
  }
  return text + '\n'
}

/**
 * Build QUEUE usage text. Matches Python QueueBot._current_usage.
 * Key differences from NODE:
 * - No access mode on locked users
 * - Shows booking queue with estimated wait times
 */
function queueUsageText(state, nodeFilter, lang) {
  let text = ''
  for (const [key, node] of Object.entries(state)) {
    if (nodeFilter && key !== nodeFilter) continue
    if (node.status === 'idle') {
      text += `${key} ${_t(lang, 'status.idle')}\n`
    } else {
      node.current_users.forEach((u, idx) => {
        const prefix = idx === 0 ? key : ''
        const rem = remainingDuration(u.start_time, u.duration)
        // QUEUE: no access mode suffix on locked users (matches Python)
        text += `${prefix} ${u.user_id}  ${formatDuration(rem, lang)}\n`
      })
    }
    // Show booking queue with estimated wait times
    const bl = node.booking_list || []
    if (bl.length > 0) {
      text += _t(lang, 'label.queue_list')
      // Calculate max remaining lock time as base wait
      let maxLockRemaining = 0
      for (const u of node.current_users || []) {
        const rem = remainingDuration(u.start_time, u.duration)
        if (rem > maxLockRemaining) maxLockRemaining = rem
      }
      let accumulatedWait = maxLockRemaining
      bl.forEach((u, idx) => {
        const dur = formatDuration(u.duration, lang)
        const waitStr = formatDuration(accumulatedWait, lang)
        text += _t(lang, 'label.queue_item', {
          index: idx + 1,
          user_id: u.user_id,
          duration: dur,
          wait_time: waitStr,
        })
        accumulatedWait += u.duration
      })
    }
  }
  return text + '\n'
}

/**
 * Build DEVICE usage text. Matches Python get_current_usage format.
 * Uses per-node headers and merged device display.
 */
function deviceUsageText(state, nodeFilter, lang) {
  let text = ''
  for (const [nodeKey, devices] of Object.entries(state)) {
    if (nodeFilter && !nodeFilter.includes(nodeKey)) continue
    text += _t(lang, 'device_usage.node_header', { node_key: nodeKey })
    // Check if heterogeneous
    const models = new Set(devices.map((d) => d.dev_model))
    const showModel = models.size > 1
    for (const dev of devices) {
      if (dev.status === 'idle') {
        const modelStr = showModel ? `${dev.dev_model} ` : ''
        text += `dev${dev.dev_id}  ${modelStr}${_t(lang, 'status.idle')}\n`
      } else {
        dev.current_users.forEach((u, idx) => {
          const devLabel = idx === 0 ? `dev${dev.dev_id}` : ''
          const modelStr = showModel && idx === 0 ? ` ${dev.dev_model}` : ''
          const rem = remainingDuration(u.start_time, u.duration)
          const uid = u.user_id + formatAccessMode(dev.status, lang)
          text += `${devLabel}${modelStr} ${uid} ${formatDuration(rem, lang)}\n`
        })
      }
    }
    text += '\n'
  }
  return text
}

// ---------------------------------------------------------------------------
// Command executor
// ---------------------------------------------------------------------------

/**
 * Execute a bot command and return the text reply.
 *
 * @param {Object} state  - Current cluster state (mutated in place)
 * @param {string} userId - User executing the command
 * @param {string} command - Raw command string (e.g. "lock node1 2h")
 * @param {string} botType - "NODE", "DEVICE", or "QUEUE"
 * @param {Object} config - Bot config
 * @param {string} lang   - Language code: 'en' or 'zh'
 * @returns {string} Text reply
 */
export function executeCommand(state, userId, command, botType, config, lang = 'en') {
  const trimmed = command.trim()

  const DEFAULT_DURATION = config.DEFAULT_DURATION || 3600
  const MAX_LOCK_DURATION = config.MAX_LOCK_DURATION || 0
  const CLUSTER_CONFIGS = config.CLUSTER_CONFIGS || {}
  const BOT_ID = config.BOT_ID || ''
  const BOT_OWNER = config.BOT_OWNER || ''
  const clusterKeys = Array.isArray(CLUSTER_CONFIGS)
    ? CLUSTER_CONFIGS
    : Object.keys(CLUSTER_CONFIGS)

  // Helper: get usage text for this bot type
  const usageText = (filter) => {
    if (botType === 'DEVICE') return deviceUsageText(state, filter, lang)
    if (botType === 'QUEUE') return queueUsageText(state, filter, lang)
    return nodeUsageText(state, filter, lang)
  }

  // Empty input -> query (matches Python handler.py)
  if (!trimmed) {
    return `${_t(lang, 'query.cluster_usage_title')}${usageText()}`
  }

  const keywords = trimmed.split(/\s+/)
  const cmd = keywords[0].toLowerCase()
  const rest = trimmed.slice(trimmed.indexOf(cmd) + cmd.length).trim()

  // Bare known node key -> query that node (matches Python handler.py)
  if (
    ![
      'lock',
      'slock',
      'unlock',
      'free',
      'kickout',
      'kicklock',
      'book',
      'take',
      'help',
      'h',
      'query',
    ].includes(cmd) &&
    clusterKeys.includes(trimmed)
  ) {
    return `${_t(lang, 'query.cluster_usage_title')}${usageText(trimmed)}`
  }

  // ---- help ----
  if (cmd === 'help' || cmd === 'h') {
    return _buildHelp(
      botType,
      CLUSTER_CONFIGS,
      DEFAULT_DURATION,
      MAX_LOCK_DURATION,
      lang,
      BOT_ID,
      BOT_OWNER
    )
  }

  // ---- query ----
  if (cmd === 'query') {
    if (rest && clusterKeys.includes(rest)) {
      return `${_t(lang, 'query.cluster_usage_title')}${usageText(rest)}`
    }
    return `${_t(lang, 'query.cluster_usage_title')}${usageText()}`
  }

  // For NODE / QUEUE: commands operate on node names
  if (botType === 'NODE' || botType === 'QUEUE') {
    return _executeNodeQueueCommand(
      state,
      userId,
      cmd,
      rest,
      botType,
      {
        DEFAULT_DURATION,
        MAX_LOCK_DURATION,
        CLUSTER_CONFIGS,
      },
      usageText,
      lang
    )
  }

  // For DEVICE: commands operate on node/dev pairs
  if (botType === 'DEVICE') {
    return _executeDeviceCommand(
      state,
      userId,
      cmd,
      rest,
      {
        DEFAULT_DURATION,
        MAX_LOCK_DURATION,
        CLUSTER_CONFIGS,
      },
      usageText,
      lang
    )
  }

  return `Unknown bot type: ${botType}`
}

// ---------------------------------------------------------------------------
// NODE / QUEUE command implementation
// ---------------------------------------------------------------------------

function _executeNodeQueueCommand(state, userId, cmd, rest, botType, cfg, usageText, lang) {
  const { DEFAULT_DURATION, MAX_LOCK_DURATION, CLUSTER_CONFIGS } = cfg
  const clusterKeys = Array.isArray(CLUSTER_CONFIGS)
    ? CLUSTER_CONFIGS
    : Object.keys(CLUSTER_CONFIGS)
  const validKeysStr = `[${clusterKeys.map((k) => `'${k}'`).join(', ')}]`

  // Helper: show error (matches Python show_error which prepends error prefix)
  const showError = (msg) => `❌${msg}`

  // ---- lock ----
  if (cmd === 'lock') {
    const durResult = parseDurationTail(rest)
    const nodePart = durResult ? durResult.rest : rest
    const duration = durResult ? durResult.seconds : DEFAULT_DURATION
    const commandHasDuration = !!durResult

    if (!nodePart) {
      return (
        _t(lang, 'error.invalid_command_format', { command: `lock ${rest}\n\n` }) +
        _buildHelp(
          botType,
          CLUSTER_CONFIGS,
          DEFAULT_DURATION,
          MAX_LOCK_DURATION,
          lang,
          BOT_ID,
          BOT_OWNER
        )
      )
    }

    const nodeKeys = splitNodeList(nodePart)
    for (const nk of nodeKeys) {
      if (!clusterKeys.includes(nk)) {
        return showError(
          _t(lang, 'error.invalid_node_key', { node_key: nk, valid_keys: validKeysStr })
        )
      }
    }

    const uniqueKeys = [...new Set(nodeKeys)]
    const timestamp = Math.floor(Date.now() / 1000)
    const usersToNotify = new Set([userId])

    // Pass 1: Validate
    for (const nk of uniqueKeys) {
      const node = state[nk]
      if (botType === 'QUEUE') {
        const bl = node.booking_list || []
        const isFirst = bl.length > 0 && bl[0].user_id === userId
        if (
          !(node.status === 'idle' && (bl.length === 0 || isFirst)) &&
          !findUser(node.current_users, userId)
        ) {
          return showError(_t(lang, 'error.node_in_use_or_not_your_turn') + `\n\n${usageText()}`)
        }
      } else {
        if (
          !(
            node.status === 'idle' ||
            (findUser(node.current_users, userId) && node.status === 'exclusive')
          )
        ) {
          return showError(_t(lang, 'error.node_in_use_or_shared') + usageText())
        }
      }
    }

    // Pass 2: Mutate
    for (const nk of uniqueKeys) {
      const node = state[nk]
      let bookingInfo = null
      if (botType === 'QUEUE') {
        bookingInfo = findUser(node.booking_list || [], userId)
        removeUser(node.booking_list, userId)
      }

      node.status = 'exclusive'
      let user = findUser(node.current_users, userId)
      let totalDuration = duration

      if (botType === 'QUEUE' && !commandHasDuration && bookingInfo) {
        totalDuration = bookingInfo.duration
      }

      if (!user) {
        user = createUser(userId, totalDuration, timestamp)
        node.current_users = [user]
      } else {
        totalDuration += user.duration
        if (botType === 'QUEUE') {
          for (const bu of node.booking_list || []) usersToNotify.add(bu.user_id)
        }
      }

      if (botType === 'QUEUE' && bookingInfo && totalDuration > bookingInfo.duration) {
        for (const bu of node.booking_list || []) usersToNotify.add(bu.user_id)
      }

      if (
        MAX_LOCK_DURATION > 0 &&
        remainingDuration(user.start_time, totalDuration) > MAX_LOCK_DURATION
      ) {
        return showError(
          _t(lang, 'error.lock_max_duration_exceeded', {
            max_duration: formatDuration(MAX_LOCK_DURATION, lang),
          })
        )
      }
      user.duration = totalDuration
      user.is_notified = false
    }

    let reply = `${_t(lang, 'success.resource_locked')}${usageText()}`
    if (botType === 'QUEUE' && usersToNotify.size > 1) {
      reply += _t(lang, 'notify.wait_time_increased')
    }
    return reply
  }

  // ---- slock ----
  if (cmd === 'slock') {
    if (botType === 'QUEUE') {
      return showError(_t(lang, 'error.slock_not_supported'))
    }

    const durResult = parseDurationTail(rest)
    const nodePart = durResult ? durResult.rest : rest
    const duration = durResult ? durResult.seconds : DEFAULT_DURATION

    if (!nodePart) {
      return (
        _t(lang, 'error.invalid_command_format', { command: `slock ${rest}\n\n` }) +
        _buildHelp(
          botType,
          CLUSTER_CONFIGS,
          DEFAULT_DURATION,
          MAX_LOCK_DURATION,
          lang,
          BOT_ID,
          BOT_OWNER
        )
      )
    }

    const nodeKeys = splitNodeList(nodePart)
    for (const nk of nodeKeys) {
      if (!clusterKeys.includes(nk)) {
        return showError(
          _t(lang, 'error.invalid_node_key', { node_key: nk, valid_keys: validKeysStr })
        )
      }
    }

    const uniqueKeys = [...new Set(nodeKeys)]
    const timestamp = Math.floor(Date.now() / 1000)

    // Pass 1: Validate
    for (const nk of uniqueKeys) {
      if (state[nk].status === 'exclusive') {
        return showError(_t(lang, 'error.node_exclusive_mode') + usageText())
      }
    }

    // Pass 2: Mutate
    for (const nk of uniqueKeys) {
      const node = state[nk]
      node.status = 'shared'
      let user = findUser(node.current_users, userId)
      if (!user) {
        user = createUser(userId, duration, timestamp)
        if (
          MAX_LOCK_DURATION > 0 &&
          remainingDuration(user.start_time, user.duration) > MAX_LOCK_DURATION
        ) {
          return showError(
            _t(lang, 'error.slock_max_duration_exceeded', {
              max_duration: formatDuration(MAX_LOCK_DURATION, lang),
            })
          )
        }
        node.current_users.push(user)
      } else {
        if (
          MAX_LOCK_DURATION > 0 &&
          remainingDuration(user.start_time, user.duration + duration) > MAX_LOCK_DURATION
        ) {
          return showError(
            _t(lang, 'error.slock_max_duration_exceeded', {
              max_duration: formatDuration(MAX_LOCK_DURATION, lang),
            })
          )
        }
        user.duration += duration
        user.is_notified = false
      }
    }

    return `${_t(lang, 'success.resource_locked')}${usageText()}`
  }

  // ---- unlock / free ----
  if (cmd === 'unlock' || cmd === 'free') {
    if (!rest) {
      for (const node of Object.values(state)) {
        removeUser(node.booking_list || [], userId)
        if (node.status !== 'idle') {
          removeUser(node.current_users, userId)
          if (node.current_users.length === 0) node.status = 'idle'
        }
      }
      return `${_t(lang, 'success.resource_released')}${usageText()}`
    }

    const nodeKeys = splitNodeList(rest)
    for (const nk of nodeKeys) {
      if (!clusterKeys.includes(nk)) {
        return showError(
          _t(lang, 'error.invalid_node_key', { node_key: nk, valid_keys: validKeysStr })
        )
      }
    }

    const uniqueKeys = [...new Set(nodeKeys)]
    for (const nk of uniqueKeys) {
      const node = state[nk]
      const hasCur = findUser(node.current_users, userId)
      const hasBook = findUser(node.booking_list || [], userId)
      if (!hasCur && !hasBook) {
        return showError(_t(lang, 'error.node_not_requested') + usageText())
      }
      removeUser(node.current_users, userId)
      removeUser(node.booking_list || [], userId)
      if (node.current_users.length === 0) node.status = 'idle'
    }

    return `${_t(lang, 'success.resource_released')}${usageText()}`
  }

  // ---- kickout ----
  if (cmd === 'kickout') {
    if (!rest) {
      return (
        _t(lang, 'error.invalid_command_format', { command: 'kickout\n\n' }) +
        _buildHelp(
          botType,
          CLUSTER_CONFIGS,
          DEFAULT_DURATION,
          MAX_LOCK_DURATION,
          lang,
          BOT_ID,
          BOT_OWNER
        )
      )
    }
    const nodeKeys = splitNodeList(rest)
    for (const nk of nodeKeys) {
      if (!clusterKeys.includes(nk)) {
        return showError(
          _t(lang, 'error.invalid_node_key', { node_key: nk, valid_keys: validKeysStr })
        )
      }
    }

    const uniqueKeys = [...new Set(nodeKeys)]

    // Collect affected users for @notification
    const affected = new Set()
    for (const nk of uniqueKeys) {
      for (const u of state[nk].current_users || []) affected.add(u.user_id)
      for (const u of state[nk].booking_list || []) affected.add(u.user_id)
    }
    affected.delete(userId)

    const before = `${_t(lang, 'success.resource_force_released', { user_id: userId })}${_t(lang, 'label.before_release')}${usageText()}`

    for (const nk of uniqueKeys) {
      state[nk].status = 'idle'
      state[nk].current_users = []
      state[nk].booking_list = []
    }

    let reply = `${before}${_t(lang, 'label.after_release')}${usageText()}`
    if (affected.size > 0) {
      reply += [...affected].map((u) => `@${u}`).join(' ') + '\n'
    }
    return reply
  }

  // ---- kicklock (QUEUE only) ----
  if (cmd === 'kicklock') {
    if (botType !== 'QUEUE') {
      return (
        _t(lang, 'error.unrecognized_command', { command: cmd + '\n\n' }) +
        _buildHelp(
          botType,
          CLUSTER_CONFIGS,
          DEFAULT_DURATION,
          MAX_LOCK_DURATION,
          lang,
          BOT_ID,
          BOT_OWNER
        )
      )
    }

    if (!rest) {
      return (
        _t(lang, 'error.invalid_command_format', { command: 'kicklock\n\n' }) +
        _buildHelp(
          botType,
          CLUSTER_CONFIGS,
          DEFAULT_DURATION,
          MAX_LOCK_DURATION,
          lang,
          BOT_ID,
          BOT_OWNER
        )
      )
    }

    const nodeKeys = splitNodeList(rest)
    for (const nk of nodeKeys) {
      if (!clusterKeys.includes(nk)) {
        return showError(
          _t(lang, 'error.invalid_node_key', { node_key: nk, valid_keys: validKeysStr })
        )
      }
    }

    const uniqueKeys = [...new Set(nodeKeys)]

    // Collect affected locked users for @notification
    const affected = new Set()
    for (const nk of uniqueKeys) {
      for (const u of state[nk].current_users || []) affected.add(u.user_id)
    }
    affected.delete(userId)

    const before = `${_t(lang, 'success.kicklock_cleared', { user_id: userId })}${_t(lang, 'label.before_release')}${usageText()}`

    for (const nk of uniqueKeys) {
      state[nk].status = 'idle'
      state[nk].current_users = []
      // Note: booking_list is NOT cleared
    }

    let reply = `${before}${_t(lang, 'label.after_release')}${usageText()}`
    if (affected.size > 0) {
      reply += [...affected].map((u) => `@${u}`).join(' ') + '\n'
    }
    return reply
  }

  // ---- book (QUEUE only) ----
  if (cmd === 'book') {
    if (botType !== 'QUEUE') {
      return (
        _t(lang, 'error.unrecognized_command', { command: 'book\n\n' }) +
        _buildHelp(
          botType,
          CLUSTER_CONFIGS,
          DEFAULT_DURATION,
          MAX_LOCK_DURATION,
          lang,
          BOT_ID,
          BOT_OWNER
        )
      )
    }

    const durResult = parseDurationTail(rest)
    const nodePart = durResult ? durResult.rest : rest
    const duration = durResult ? durResult.seconds : DEFAULT_DURATION

    if (!nodePart) {
      return (
        _t(lang, 'error.invalid_command_format', { command: `book ${rest}\n\n` }) +
        _buildHelp(
          botType,
          CLUSTER_CONFIGS,
          DEFAULT_DURATION,
          MAX_LOCK_DURATION,
          lang,
          BOT_ID,
          BOT_OWNER
        )
      )
    }

    const nodeKeys = splitNodeList(nodePart)
    for (const nk of nodeKeys) {
      if (!clusterKeys.includes(nk)) {
        return showError(
          _t(lang, 'error.invalid_node_key', { node_key: nk, valid_keys: validKeysStr })
        )
      }
    }

    const uniqueKeys = [...new Set(nodeKeys)]
    const timestamp = Math.floor(Date.now() / 1000)

    for (const nk of uniqueKeys) {
      const node = state[nk]
      if (findUser(node.current_users, userId) || findUser(node.booking_list || [], userId)) {
        return showError(_t(lang, 'error.already_locked') + `\n${usageText()}`)
      }
      if (MAX_LOCK_DURATION > 0 && duration > MAX_LOCK_DURATION) {
        return showError(
          _t(lang, 'error.lock_max_duration_exceeded', {
            max_duration: formatDuration(MAX_LOCK_DURATION, lang),
          })
        )
      }
      if (!node.booking_list) node.booking_list = []
      node.booking_list.push(createUser(userId, duration, timestamp))
    }

    return `${_t(lang, 'success.booking_added')}${usageText()}`
  }

  // ---- take (QUEUE only) ----
  if (cmd === 'take') {
    if (botType !== 'QUEUE') {
      return (
        _t(lang, 'error.unrecognized_command', { command: 'take\n\n' }) +
        _buildHelp(
          botType,
          CLUSTER_CONFIGS,
          DEFAULT_DURATION,
          MAX_LOCK_DURATION,
          lang,
          BOT_ID,
          BOT_OWNER
        )
      )
    }

    const durResult = parseDurationTail(rest)
    const nodePart = durResult ? durResult.rest : rest
    const duration = durResult ? durResult.seconds : DEFAULT_DURATION

    if (!nodePart) {
      return (
        _t(lang, 'error.invalid_command_format', { command: `take ${rest}\n\n` }) +
        _buildHelp(
          botType,
          CLUSTER_CONFIGS,
          DEFAULT_DURATION,
          MAX_LOCK_DURATION,
          lang,
          BOT_ID,
          BOT_OWNER
        )
      )
    }

    const nodeKeys = splitNodeList(nodePart)
    for (const nk of nodeKeys) {
      if (!clusterKeys.includes(nk)) {
        return showError(
          _t(lang, 'error.invalid_node_key', { node_key: nk, valid_keys: validKeysStr })
        )
      }
    }

    const uniqueKeys = [...new Set(nodeKeys)]
    const timestamp = Math.floor(Date.now() / 1000)

    // Capture before state and affected users
    const beforeText = usageText()
    const preempted = new Set()

    for (const nk of uniqueKeys) {
      const node = state[nk]
      if (findUser(node.current_users, userId)) {
        return showError(_t(lang, 'error.locked_user_cannot_take') + `\n${usageText()}`)
      }

      // Collect preempted users for @notification
      for (const u of node.current_users) preempted.add(u.user_id)

      node.status = 'exclusive'
      removeUser(node.booking_list || [], userId)

      const others = node.current_users.filter((u) => u.user_id !== userId)
      if (!node.booking_list) node.booking_list = []
      for (const other of others.reverse()) {
        const rem = remainingDuration(other.start_time, other.duration)
        if (rem > 0) {
          other.start_time = timestamp
          other.duration = rem
          other.is_notified = false
          node.booking_list.unshift(other)
        }
      }
      for (const u of node.booking_list) u.is_notified = false

      if (MAX_LOCK_DURATION > 0 && remainingDuration(timestamp, duration) > MAX_LOCK_DURATION) {
        return showError(
          _t(lang, 'error.lock_max_duration_exceeded', {
            max_duration: formatDuration(MAX_LOCK_DURATION, lang),
          })
        )
      }

      const user = createUser(userId, duration, timestamp)
      node.current_users = [user]
    }

    preempted.delete(userId)
    let reply = `${_t(lang, 'success.take_success_by', { user_id: userId })}${_t(lang, 'label.before_take')}${beforeText}\n${_t(lang, 'label.after_take')}${usageText()}`
    if (preempted.size > 0) {
      reply += [...preempted].map((u) => `@${u}`).join(' ') + '\n'
    }
    return reply
  }

  // Unrecognized command -> show help
  return (
    // eslint-disable-next-line no-undef
    _t(lang, 'error.unrecognized_command', { command: trimmed + '\n\n' }) +
    _buildHelp(
      botType,
      CLUSTER_CONFIGS,
      DEFAULT_DURATION,
      MAX_LOCK_DURATION,
      lang,
      BOT_ID,
      BOT_OWNER
    )
  )
}

// ---------------------------------------------------------------------------
// DEVICE command implementation
// ---------------------------------------------------------------------------

function _executeDeviceCommand(state, userId, cmd, rest, cfg, usageText, lang) {
  const { DEFAULT_DURATION, MAX_LOCK_DURATION, CLUSTER_CONFIGS } = cfg
  const validKeysStr = `[${Object.keys(CLUSTER_CONFIGS)
    .map((k) => `'${k}'`)
    .join(', ')}]`

  const showError = (msg) => `❌${msg}`

  // ---- lock / slock ----
  if (cmd === 'lock' || cmd === 'slock') {
    const isShared = cmd === 'slock'
    const durResult = parseDurationTail(rest)
    const nodePart = durResult ? durResult.rest : rest
    const duration = durResult ? durResult.seconds : DEFAULT_DURATION

    const tokens = nodePart.match(/^([\w-]+)((\s*[,，、]\s*[\w-]+)*)\s*(.*)?$/)
    if (!tokens) {
      return (
        _t(lang, 'error.invalid_command_format', { command: '\n\n' }) +
        _buildHelp(
          'DEVICE',
          CLUSTER_CONFIGS,
          DEFAULT_DURATION,
          MAX_LOCK_DURATION,
          lang,
          BOT_ID,
          BOT_OWNER
        )
      )
    }

    const rawNodes = (tokens[1] + (tokens[2] || '')).trim()
    const devRemainder = (tokens[4] || '').trim()
    const nodeKeys = splitNodeList(rawNodes)

    for (const nk of nodeKeys) {
      if (!(nk in CLUSTER_CONFIGS)) {
        return showError(
          _t(lang, 'error.invalid_node_key', { node_key: nk, valid_keys: validKeysStr })
        )
      }
    }

    const uniqueKeys = [...new Set(nodeKeys)]
    const timestamp = Math.floor(Date.now() / 1000)

    const nodeDevIds = {}
    for (const nk of uniqueKeys) {
      nodeDevIds[nk] =
        parseDevSpec(devRemainder, CLUSTER_CONFIGS, nk) || CLUSTER_CONFIGS[nk].map((_, i) => i)
    }

    // Pass 1: Validate
    for (const nk of uniqueKeys) {
      const devices = state[nk]
      for (const devId of nodeDevIds[nk]) {
        const dev = devices[devId]
        if (!dev) continue
        if (isShared) {
          if (dev.status === 'exclusive') {
            return showError(_t(lang, 'error.device_exclusive_mode') + usageText([nk]))
          }
        } else {
          if (
            !(
              dev.status === 'idle' ||
              (findUser(dev.current_users, userId) && dev.status === 'exclusive')
            )
          ) {
            return showError(_t(lang, 'error.device_in_use_or_shared') + usageText([nk]))
          }
        }
      }
    }

    // Pass 2: Mutate
    for (const nk of uniqueKeys) {
      const devices = state[nk]
      for (const devId of nodeDevIds[nk]) {
        const dev = devices[devId]
        if (!dev) continue

        if (isShared) {
          dev.status = 'shared'
          let user = findUser(dev.current_users, userId)
          if (!user) {
            user = createUser(userId, duration, timestamp)
            if (
              MAX_LOCK_DURATION > 0 &&
              remainingDuration(user.start_time, user.duration) > MAX_LOCK_DURATION
            ) {
              return showError(
                _t(lang, 'error.slock_max_duration_exceeded', {
                  max_duration: formatDuration(MAX_LOCK_DURATION, lang),
                })
              )
            }
            dev.current_users.push(user)
          } else {
            if (
              MAX_LOCK_DURATION > 0 &&
              remainingDuration(user.start_time, user.duration + duration) > MAX_LOCK_DURATION
            ) {
              return showError(
                _t(lang, 'error.slock_max_duration_exceeded', {
                  max_duration: formatDuration(MAX_LOCK_DURATION, lang),
                })
              )
            }
            user.duration += duration
            user.is_notified = false
          }
        } else {
          dev.status = 'exclusive'
          let user = findUser(dev.current_users, userId)
          let totalDuration = duration
          if (!user) {
            user = createUser(userId, totalDuration, timestamp)
            dev.current_users = [user]
          } else {
            totalDuration += user.duration
          }
          if (
            MAX_LOCK_DURATION > 0 &&
            remainingDuration(user.start_time, totalDuration) > MAX_LOCK_DURATION
          ) {
            return showError(
              _t(lang, 'error.lock_max_duration_exceeded', {
                max_duration: formatDuration(MAX_LOCK_DURATION, lang),
              })
            )
          }
          user.duration = totalDuration
          user.is_notified = false
        }
      }
    }

    return `${_t(lang, 'success.resource_locked')}${usageText()}`
  }

  // ---- unlock / free ----
  if (cmd === 'unlock' || cmd === 'free') {
    if (!rest) {
      for (const devices of Object.values(state)) {
        for (const dev of devices) {
          if (dev.status !== 'idle') {
            removeUser(dev.current_users, userId)
            if (dev.current_users.length === 0) dev.status = 'idle'
          }
        }
      }
      return `${_t(lang, 'success.resource_released')}${usageText()}`
    }

    const tokens = rest.match(/^([\w-]+)((\s*[,，、]\s*[\w-]+)*)\s*(.*)?$/)
    if (!tokens) {
      return (
        _t(lang, 'error.invalid_command_format', { command: '\n\n' }) +
        _buildHelp(
          'DEVICE',
          CLUSTER_CONFIGS,
          DEFAULT_DURATION,
          MAX_LOCK_DURATION,
          lang,
          BOT_ID,
          BOT_OWNER
        )
      )
    }

    const rawNodes = (tokens[1] + (tokens[2] || '')).trim()
    const devRemainder = (tokens[4] || '').trim()
    const nodeKeys = splitNodeList(rawNodes)

    for (const nk of nodeKeys) {
      if (!(nk in CLUSTER_CONFIGS)) {
        return showError(
          _t(lang, 'error.invalid_node_key', { node_key: nk, valid_keys: validKeysStr })
        )
      }
    }

    const uniqueKeys = [...new Set(nodeKeys)]

    for (const nk of uniqueKeys) {
      const devIds = parseDevSpec(devRemainder, CLUSTER_CONFIGS, nk)
      const devices = state[nk]

      if (!devIds) {
        for (const dev of devices) {
          if (dev.status !== 'idle') {
            removeUser(dev.current_users, userId)
            if (dev.current_users.length === 0) dev.status = 'idle'
          }
        }
      } else {
        for (const devId of devIds) {
          const dev = devices[devId]
          if (!dev) continue
          if (!findUser(dev.current_users, userId)) {
            return showError(_t(lang, 'error.device_not_requested') + usageText([nk]))
          }
          removeUser(dev.current_users, userId)
          if (dev.current_users.length === 0) dev.status = 'idle'
        }
      }
    }

    return `${_t(lang, 'success.resource_released')}${usageText()}`
  }

  // ---- kickout ----
  if (cmd === 'kickout') {
    const tokens = rest.match(/^([\w-]+)((\s*[,，、]\s*[\w-]+)*)\s*(.*)?$/)
    if (!tokens) {
      return (
        _t(lang, 'error.invalid_command_format', { command: '\n\n' }) +
        _buildHelp(
          'DEVICE',
          CLUSTER_CONFIGS,
          DEFAULT_DURATION,
          MAX_LOCK_DURATION,
          lang,
          BOT_ID,
          BOT_OWNER
        )
      )
    }

    const rawNodes = (tokens[1] + (tokens[2] || '')).trim()
    const devRemainder = (tokens[4] || '').trim()
    const nodeKeys = splitNodeList(rawNodes)

    for (const nk of nodeKeys) {
      if (!(nk in CLUSTER_CONFIGS)) {
        return showError(
          _t(lang, 'error.invalid_node_key', { node_key: nk, valid_keys: validKeysStr })
        )
      }
    }

    const uniqueKeys = [...new Set(nodeKeys)]

    // Collect affected users for @notification
    const affected = new Set()
    for (const nk of uniqueKeys) {
      const devIds = parseDevSpec(devRemainder, CLUSTER_CONFIGS, nk)
      const devices = state[nk]
      if (!devIds) {
        for (const dev of devices) {
          for (const u of dev.current_users) affected.add(u.user_id)
        }
      } else {
        for (const devId of devIds) {
          const dev = devices[devId]
          if (dev) for (const u of dev.current_users) affected.add(u.user_id)
        }
      }
    }
    affected.delete(userId)

    const before = `${_t(lang, 'success.resource_force_released', { user_id: userId })}${_t(lang, 'label.before_release')}${usageText(uniqueKeys)}`

    for (const nk of uniqueKeys) {
      const devIds = parseDevSpec(devRemainder, CLUSTER_CONFIGS, nk)
      const devices = state[nk]

      if (!devIds) {
        for (const dev of devices) {
          dev.status = 'idle'
          dev.current_users = []
        }
      } else {
        for (const devId of devIds) {
          const dev = devices[devId]
          if (dev) {
            dev.status = 'idle'
            dev.current_users = []
          }
        }
      }
    }

    let reply = `${before}\n${_t(lang, 'label.after_release')}${usageText(uniqueKeys)}`
    if (affected.size > 0) {
      reply += [...affected].map((u) => `@${u}`).join(' ') + '\n'
    }
    return reply
  }

  // Unrecognized command
  return (
    _t(lang, 'error.unrecognized_command', { command: cmd + '\n\n' }) +
    _buildHelp(
      'DEVICE',
      CLUSTER_CONFIGS,
      DEFAULT_DURATION,
      MAX_LOCK_DURATION,
      lang,
      BOT_ID,
      BOT_OWNER
    )
  )
}

// ---------------------------------------------------------------------------
// Help text builder  (matches Python print_help / _help_header / _help_commands)
// ---------------------------------------------------------------------------

function _buildHelp(
  botType,
  CLUSTER_CONFIGS,
  DEFAULT_DURATION,
  MAX_LOCK_DURATION,
  lang,
  botId,
  botOwner
) {
  const clusterKeys = Array.isArray(CLUSTER_CONFIGS)
    ? CLUSTER_CONFIGS
    : Object.keys(CLUSTER_CONFIGS)
  const ex0 = clusterKeys[0] || 'node0'
  const ex1 = clusterKeys[1] || null

  let text = _t(lang, 'help.title')
  text += _t(lang, 'help.section1_title')
  text += _t(lang, 'help.rule1_default_duration', {
    default_duration: formatDuration(DEFAULT_DURATION, lang),
  })
  text += _t(lang, 'help.rule2_post_expiry_notification')

  if (botType === 'DEVICE') {
    text += _t(lang, 'help.rule3_lock_modes')
    text += _t(lang, 'help.lock_all_devices_example', { node: ex0 })
    text += _t(lang, 'help.lock_device_example', { node: ex0 })
    text += _t(lang, 'help.lock_device_duration_example', { node: ex0 })
    text += _t(lang, 'help.lock_device_range_example', { node: ex0 })
    text += _t(lang, 'help.slock_device_range_example', { node: ex0 })
    text += _t(lang, 'help.section2_title')
    text += _t(lang, 'help.unlock_device_example', { node: ex0 })
    text += _t(lang, 'help.unlock_device_range_example', { node: ex0 })
    text += _t(lang, 'help.free_device_range_example', { node: ex0 })
    text += _t(lang, 'help.free_node_all_example', { node: ex0 })
    text += _t(lang, 'help.free_all')
    text += _t(lang, 'help.section3_title')
    text += _t(lang, 'help.kickout_device_example', { node: ex0 })
    text += _t(lang, 'help.kickout_device_range_example', { node: ex0 })
    text += _t(lang, 'help.kickout_device_range2_example', { node: ex0 })
    text += _t(lang, 'help.section4_title')
    text += _t(lang, 'help.section5_title')
    text += _t(lang, 'help.query_at_bot')
    text += _t(lang, 'help.query_node_example', { node: ex0 })
    text += '\n' + _t(lang, 'help.resource_list_title')
    for (const [nk, devs] of Object.entries(CLUSTER_CONFIGS)) {
      text += _t(lang, 'help.resource_list_item', { node_key: nk, max_dev: devs.length - 1 })
    }
  } else if (botType === 'QUEUE') {
    text += _t(lang, 'help.rule3_lock_exclusive')
    text += _t(lang, 'help.lock_example', { node: ex0 })
    text += _t(lang, 'help.lock_duration_example', { node: ex0, duration: '3d' })
    if (ex1)
      text += _t(lang, 'help.lock_duration_example', { node: `${ex0},${ex1}`, duration: '2h' })
    text += _t(lang, 'help.slock_example', { node: ex0 })
    text += _t(lang, 'help.section2_title')
    text += _t(lang, 'help.unlock_example', { node: ex0 })
    if (ex1) text += _t(lang, 'help.unlock_example', { node: `${ex0},${ex1}` })
    text += _t(lang, 'help.free_all')
    text += _t(lang, 'help.section_kickout_title')
    text += _t(lang, 'help.kickout_example', { node: ex0 })
    if (ex1) text += _t(lang, 'help.kickout_example', { node: `${ex0},${ex1}` })
    text += _t(lang, 'help.section_booking_title')
    text += _t(lang, 'help.book_duration_example', { node: ex0, duration: '2h' })
    text += _t(lang, 'help.section_cancel_booking_title')
    text += _t(lang, 'help.unlock_example', { node: ex0 })
    text += _t(lang, 'help.section_take_title')
    text += _t(lang, 'help.take_example', { node: ex0 })
    text += _t(lang, 'help.section_kicklock_title')
    text += _t(lang, 'help.kickout_example', { node: ex0 })
    text += _t(lang, 'help.section_help_title_queue')
    text += _t(lang, 'help.section_query_title_queue')
    text += _t(lang, 'help.query_at_bot')
    text += `    ${ex0}\n`
  } else {
    // NODE
    text += _t(lang, 'help.rule3_lock_modes')
    text += _t(lang, 'help.lock_example', { node: ex0 })
    text += _t(lang, 'help.lock_duration_example', { node: ex0, duration: '3d' })
    if (ex1)
      text += _t(lang, 'help.lock_duration_example', { node: `${ex0},${ex1}`, duration: '2h' })
    text += _t(lang, 'help.slock_example', { node: ex0 })
    text += _t(lang, 'help.section2_title')
    text += _t(lang, 'help.unlock_example', { node: ex0 })
    if (ex1) text += _t(lang, 'help.unlock_example', { node: `${ex0},${ex1}` })
    text += _t(lang, 'help.free_all')
    text += _t(lang, 'help.section3_title')
    text += _t(lang, 'help.kickout_example', { node: ex0 })
    if (ex1) text += _t(lang, 'help.kickout_example', { node: `${ex0},${ex1}` })
    text += _t(lang, 'help.section4_title')
    text += _t(lang, 'help.section5_title')
    text += _t(lang, 'help.query_at_bot')
    text += `    ${ex0}\n`
  }

  text += '\n'
  if (MAX_LOCK_DURATION > 0) {
    text += _t(lang, 'help.max_duration_warning', {
      max_duration: formatDuration(MAX_LOCK_DURATION, lang),
    })
  }
  text += _t(lang, 'help.bot_version', { version: '2.0.0' })
  if (botId) {
    text += _t(lang, 'help.bot_id', { bot_id: botId })
  }
  if (botOwner) {
    text += _t(lang, 'help.bot_owner', { owner: botOwner })
  }
  text += '\n'
  return text
}
