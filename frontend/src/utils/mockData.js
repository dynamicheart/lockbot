/**
 * Seed data for the demo / GitHub Pages mode.
 *
 * All mutable state is kept in plain objects that api.mock.js reads
 * and writes so that the UI feels fully interactive.
 */

const now = '2026-04-09T00:00:00'

// ---------------------------------------------------------------------------
// Users
// ---------------------------------------------------------------------------
export const mockUsers = [
  {
    id: 1,
    username: 'demo_user',
    email: 'demo@example.com',
    role: 'super_admin',
    max_running_bots: 10,
    must_change_password: false,
    created_at: now,
    updated_at: now,
  },
  {
    id: 2,
    username: 'admin',
    email: 'admin@example.com',
    role: 'admin',
    max_running_bots: 10,
    must_change_password: false,
    created_at: now,
    updated_at: now,
  },
  {
    id: 3,
    username: 'user1',
    email: 'user1@example.com',
    role: 'user',
    max_running_bots: 5,
    must_change_password: false,
    created_at: now,
    updated_at: now,
  },
  {
    id: 4,
    username: 'user2',
    email: 'user2@example.com',
    role: 'user',
    max_running_bots: 3,
    must_change_password: false,
    created_at: now,
    updated_at: now,
  },
  {
    id: 5,
    username: 'researcher',
    email: 'researcher@example.com',
    role: 'admin',
    max_running_bots: 8,
    must_change_password: false,
    created_at: now,
    updated_at: now,
  },
  {
    id: 6,
    username: 'intern',
    email: 'intern@example.com',
    role: 'user',
    max_running_bots: 2,
    must_change_password: false,
    created_at: now,
    updated_at: now,
  },
]

// ---------------------------------------------------------------------------
// Bots
// ---------------------------------------------------------------------------
export const mockBots = [
  // ── demo_user (id 1, super_admin) ──
  {
    id: 1,
    user_id: 1,
    name: 'GPU Cluster',
    name_i18n: 'demoBot.gpuCluster',
    bot_type: 'DEVICE',
    platform: 'infoflow',
    group_id: null,
    last_user_id: null,
    status: 'running',
    last_request_at: null,
    cluster_configs: JSON.stringify({ node1: ['A100', 'A100', 'H100'], node2: ['A100'] }),
    config_overrides: JSON.stringify({ LANGUAGE: 'en' }),
    created_at: now,
    updated_at: now,
  },
  {
    id: 2,
    user_id: 1,
    name: 'Compute Nodes',
    name_i18n: 'demoBot.computeNodes',
    bot_type: 'NODE',
    platform: 'infoflow',
    group_id: null,
    last_user_id: null,
    status: 'running',
    last_request_at: null,
    cluster_configs: JSON.stringify(['compute-1', 'compute-2', 'compute-3']),
    config_overrides: null,
    created_at: now,
    updated_at: now,
  },
  {
    id: 3,
    user_id: 1,
    name: 'Training Queue',
    name_i18n: 'demoBot.trainingQueue',
    bot_type: 'QUEUE',
    platform: 'infoflow',
    group_id: null,
    last_user_id: null,
    status: 'running',
    last_request_at: null,
    cluster_configs: JSON.stringify(['queue-a', 'queue-b']),
    config_overrides: null,
    created_at: now,
    updated_at: now,
  },
  {
    id: 4,
    user_id: 1,
    name: 'Dev GPU',
    name_i18n: 'demoBot.devGpu',
    bot_type: 'DEVICE',
    platform: 'infoflow',
    group_id: null,
    last_user_id: null,
    status: 'stopped',
    last_request_at: null,
    cluster_configs: JSON.stringify({ 'dev-node': ['V100', 'V100'] }),
    config_overrides: null,
    created_at: now,
    updated_at: now,
  },
  // ── researcher (id 5, admin) ──
  {
    id: 5,
    user_id: 5,
    name: 'Mixed GPU Cluster',
    name_i18n: 'demoBot.labGpus',
    bot_type: 'DEVICE',
    platform: 'infoflow',
    group_id: null,
    last_user_id: null,
    status: 'running',
    last_request_at: null,
    cluster_configs: JSON.stringify({ 'srv-a': ['H20', 'H20', 'A100', 'A100'], 'srv-b': ['H100', 'H100', 'A100'] }),
    config_overrides: JSON.stringify({ LANGUAGE: 'en' }),
    created_at: now,
    updated_at: now,
  },
  {
    id: 6,
    user_id: 5,
    name: 'Queue Bot',
    name_i18n: 'demoBot.evalQueue',
    bot_type: 'QUEUE',
    platform: 'infoflow',
    group_id: null,
    last_user_id: null,
    status: 'running',
    last_request_at: null,
    cluster_configs: JSON.stringify(['eval-1', 'eval-2']),
    config_overrides: null,
    created_at: now,
    updated_at: now,
  },
  // ── intern (id 6, user) ──
  {
    id: 7,
    user_id: 6,
    name: 'Shared Nodes',
    name_i18n: 'demoBot.sharedNodes',
    bot_type: 'NODE',
    platform: 'infoflow',
    group_id: null,
    last_user_id: null,
    status: 'running',
    last_request_at: null,
    cluster_configs: JSON.stringify(['shared-1', 'shared-2']),
    config_overrides: null,
    created_at: now,
    updated_at: now,
  },
]

// ---------------------------------------------------------------------------
// Cluster state for running bots (bot ids 1 and 2)
// ---------------------------------------------------------------------------
const _ts = Math.floor(Date.now() / 1000)
const _oneHour = 3600
const _twoHours = 7200
const _threeHours = 10800

// DEVICE state: each node maps to an array of device objects
const _deviceStateBot1 = {
  node1: [
    {
      dev_id: 0,
      dev_model: 'A100',
      status: 'exclusive',
      current_users: [
        { user_id: 'user1', start_time: _ts - _oneHour, duration: _threeHours, is_notified: false },
      ],
    },
    {
      dev_id: 1,
      dev_model: 'A100',
      status: 'shared',
      current_users: [
        { user_id: 'user1', start_time: _ts - 1800, duration: _twoHours, is_notified: false },
        { user_id: 'user2', start_time: _ts - 900, duration: _oneHour, is_notified: false },
      ],
    },
    {
      dev_id: 2,
      dev_model: 'H100',
      status: 'idle',
      current_users: [],
    },
  ],
  node2: [
    {
      dev_id: 0,
      dev_model: 'A100',
      status: 'exclusive',
      current_users: [
        { user_id: 'demo_user', start_time: _ts - _twoHours, duration: _threeHours, is_notified: false },
      ],
    },
  ],
}

// NODE state: each node is { status, current_users, booking_list }
const _nodeStateBot2 = {
  'compute-1': {
    status: 'exclusive',
    current_users: [
      { user_id: 'user1', start_time: _ts - _oneHour, duration: _twoHours, is_notified: false },
    ],
    booking_list: [],
  },
  'compute-2': {
    status: 'shared',
    current_users: [
      { user_id: 'user1', start_time: _ts - 1200, duration: _threeHours, is_notified: false },
      { user_id: 'user2', start_time: _ts - 600, duration: _oneHour, is_notified: false },
    ],
    booking_list: [],
  },
  'compute-3': {
    status: 'idle',
    current_users: [],
    booking_list: [],
  },
}

// QUEUE state: each node is { status, current_users, booking_list }
const _queueStateBot3 = {
  'queue-a': {
    status: 'exclusive',
    current_users: [
      { user_id: 'user1', start_time: _ts - _oneHour, duration: _twoHours, is_notified: false },
    ],
    booking_list: [
      { user_id: 'user2', start_time: _ts - 600, duration: _oneHour, is_notified: false },
      { user_id: 'demo_user', start_time: _ts - 300, duration: _threeHours, is_notified: false },
    ],
  },
  'queue-b': {
    status: 'idle',
    current_users: [],
    booking_list: [],
  },
}

// DEVICE state for Mixed GPU Cluster (bot 5) — H20/A100/H100 heterogeneous
const _deviceStateBot5 = {
  'srv-a': [
    {
      dev_id: 0,
      dev_model: 'H20',
      status: 'exclusive',
      current_users: [
        { user_id: 'intern', start_time: _ts - _oneHour, duration: _twoHours, is_notified: false },
      ],
    },
    {
      dev_id: 1,
      dev_model: 'H20',
      status: 'idle',
      current_users: [],
    },
    {
      dev_id: 2,
      dev_model: 'A100',
      status: 'shared',
      current_users: [
        { user_id: 'researcher', start_time: _ts - 1800, duration: _threeHours, is_notified: false },
        { user_id: 'user1', start_time: _ts - 600, duration: _oneHour, is_notified: false },
      ],
    },
    {
      dev_id: 3,
      dev_model: 'A100',
      status: 'idle',
      current_users: [],
    },
  ],
  'srv-b': [
    {
      dev_id: 0,
      dev_model: 'H100',
      status: 'exclusive',
      current_users: [
        { user_id: 'demo_user', start_time: _ts - _twoHours, duration: _threeHours, is_notified: false },
      ],
    },
    {
      dev_id: 1,
      dev_model: 'H100',
      status: 'idle',
      current_users: [],
    },
    {
      dev_id: 2,
      dev_model: 'A100',
      status: 'idle',
      current_users: [],
    },
  ],
}

// QUEUE state for Queue Bot (bot 6)
const _queueStateBot6 = {
  'eval-1': {
    status: 'exclusive',
    current_users: [
      { user_id: 'researcher', start_time: _ts - 1800, duration: _twoHours, is_notified: false },
    ],
    booking_list: [
      { user_id: 'intern', start_time: _ts - 300, duration: _oneHour, is_notified: false },
    ],
  },
  'eval-2': {
    status: 'idle',
    current_users: [],
    booking_list: [],
  },
}

// NODE state for intern's Shared Nodes (bot 7)
const _nodeStateBot7 = {
  'shared-1': {
    status: 'shared',
    current_users: [
      { user_id: 'intern', start_time: _ts - 1200, duration: _twoHours, is_notified: false },
      { user_id: 'user1', start_time: _ts - 600, duration: _oneHour, is_notified: false },
    ],
    booking_list: [],
  },
  'shared-2': {
    status: 'idle',
    current_users: [],
    booking_list: [],
  },
}

/**
 * botStates[botId] is the live cluster state for that bot.
 * Only populated for running bots at seed time.
 */
export const botStates = {
  1: _deviceStateBot1,
  2: _nodeStateBot2,
  3: _queueStateBot3,
  5: _deviceStateBot5,
  6: _queueStateBot6,
  7: _nodeStateBot7,
}

// ---------------------------------------------------------------------------
// Log entries
// ---------------------------------------------------------------------------
let _logIdCounter = 100

function _makeLog(botId, category, level, message, minutesAgo = 0) {
  const d = new Date(Date.now() - minutesAgo * 60 * 1000)
  return {
    id: _logIdCounter++,
    bot_id: botId,
    category,
    level,
    message,
    created_at: d.toISOString(),
  }
}

export const mockLogs = [
  _makeLog(1, 'system', 'INFO', 'Bot started', 120),
  _makeLog(1, 'command', 'INFO', '[user1] lock node1 dev0 3h', 115),
  _makeLog(1, 'command', 'INFO', '[user1] slock node1 dev1 2h', 30),
  _makeLog(1, 'command', 'INFO', '[demo_user] lock node2 3h', 120),
  _makeLog(1, 'system', 'INFO', 'Bot language set to en', 60),

  _makeLog(2, 'system', 'INFO', 'Bot started', 90),
  _makeLog(2, 'command', 'INFO', '[user1] lock compute-1 2h', 60),
  _makeLog(2, 'command', 'INFO', '[user1] slock compute-2 3h', 20),
  _makeLog(2, 'command', 'INFO', '[user2] slock compute-2 1h', 10),

  _makeLog(3, 'system', 'INFO', 'Bot started', 60),
  _makeLog(3, 'command', 'INFO', '[user1] lock queue-a 2h', 60),
  _makeLog(3, 'command', 'INFO', '[user2] book queue-a 1h', 10),
  _makeLog(3, 'command', 'INFO', '[demo_user] book queue-a 3h', 5),

  _makeLog(4, 'system', 'INFO', 'Bot created', 60),
  _makeLog(4, 'system', 'INFO', 'Bot stopped', 55),

  _makeLog(5, 'system', 'INFO', 'Bot started', 80),
  _makeLog(5, 'command', 'INFO', '[intern] lock srv-a 0 2h', 60),
  _makeLog(5, 'command', 'INFO', '[researcher] slock srv-a 2 3h', 30),
  _makeLog(5, 'command', 'INFO', '[demo_user] lock srv-b 0 3h', 120),

  _makeLog(6, 'system', 'INFO', 'Bot started', 70),
  _makeLog(6, 'command', 'INFO', '[researcher] lock eval-1 2h', 30),
  _makeLog(6, 'command', 'INFO', '[intern] book eval-1 1h', 5),

  _makeLog(7, 'system', 'INFO', 'Bot started', 50),
  _makeLog(7, 'command', 'INFO', '[intern] slock shared-1 2h', 20),
  _makeLog(7, 'command', 'INFO', '[user1] slock shared-1 1h', 10),
]

/**
 * Helper to add a new log entry at runtime.
 */
export function appendLog(botId, category, level, message) {
  const entry = _makeLog(botId, category, level, message, 0)
  mockLogs.push(entry)
  return entry
}

/**
 * Get the next bot id (for creating new bots).
 */
export function nextBotId() {
  return Math.max(...mockBots.map((b) => b.id), 0) + 1
}
