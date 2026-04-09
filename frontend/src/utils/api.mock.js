/**
 * Mock API adapter — drop-in replacement for the axios instance.
 *
 * Implements the same interface: get(url, config), post(url, data, config),
 * put(url, data, config), delete(url, config).  Every method returns
 * Promise.resolve({ data: ... }) with a simulated 200-400 ms delay.
 *
 * All mutable state lives in mockData.js so that multiple views see
 * consistent data.
 */
import {
  mockUsers,
  mockBots,
  botStates,
  mockLogs,
  appendLog,
  nextBotId,
} from './mockData'
import { validateBotState } from './stateValidation'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Simulate network latency (200-400 ms). */
function _delay() {
  const ms = 200 + Math.random() * 200
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** Extract path params from a URL pattern like /bots/42/state. */
function _match(url, pattern) {
  const urlParts = url.split('?')[0].split('/')
  const patParts = pattern.split('/')
  if (urlParts.length !== patParts.length) return null
  const params = {}
  for (let i = 0; i < patParts.length; i++) {
    if (patParts[i].startsWith(':')) {
      params[patParts[i].slice(1)] = urlParts[i]
    } else if (urlParts[i] !== patParts[i]) {
      return null
    }
  }
  return params
}

/** Return the currently logged-in user (via the mock token). */
function _currentUser() {
  const token = localStorage.getItem('token')
  if (token === 'demo-token') return mockUsers.find((u) => u.username === 'demo_user')
  // If a different user token is stored, try to find them
  const userId = parseInt(token, 10)
  if (userId && !isNaN(userId)) return mockUsers.find((u) => u.id === userId)
  return null
}

function _isAdmin(user) {
  return user && (user.role === 'admin' || user.role === 'super_admin')
}

function _isSuperAdmin(user) {
  return user && user.role === 'super_admin'
}

// ---------------------------------------------------------------------------
// Mock API
// ---------------------------------------------------------------------------

const mockApi = {
  async get(url, config) {
    await _delay()
    const params = (config && config.params) || {}
    return { data: _handleGet(url, params) }
  },

  async post(url, data, config) {
    await _delay()
    return { data: _handlePost(url, data || {}) }
  },

  async put(url, data, config) {
    await _delay()
    return { data: _handlePut(url, data || {}) }
  },

  async delete(url, config) {
    await _delay()
    _handleDelete(url)
    return { data: null }
  },
}

// ---------------------------------------------------------------------------
// GET handler
// ---------------------------------------------------------------------------

function _handleGet(url, params) {
  // ── Auth ──
  if (url === '/auth/me') {
    const user = _currentUser()
    if (!user) throw _err(401, 'Not authenticated')
    return user
  }

  // ── Bots ──
  if (url === '/bots') {
    const user = _currentUser()
    if (!user) throw _err(401, 'Not authenticated')
    return mockBots.filter((b) => b.user_id === user.id)
  }

  if (url === '/bots/running-states') {
    const user = _currentUser()
    if (!user) throw _err(401, 'Not authenticated')
    const result = {}
    for (const bot of mockBots) {
      if (bot.user_id === user.id && bot.status === 'running' && botStates[bot.id]) {
        result[bot.id] = botStates[bot.id]
      }
    }
    return result
  }

  let m
  // GET /bots/:id
  m = _match(url, '/bots/:id')
  if (m) {
    const bot = mockBots.find((b) => b.id === parseInt(m.id))
    if (!bot) throw _err(404, 'Bot not found')
    const user = _currentUser()
    if (!user) throw _err(401, 'Not authenticated')
    if (bot.user_id !== user.id && !_isAdmin(user)) throw _err(404, 'Bot not found')
    const owner = mockUsers.find((u) => u.id === bot.user_id)
    return {
      ...bot,
      owner: owner ? owner.username : '',
      owner_role: owner ? owner.role : '',
      webhook_url_raw: 'https://demo.example.com/webhook',
      aes_key_raw: 'demo_aes_key_placeholder',
      token_raw: 'demo_token_placeholder',
      webhook_url_masked: 'https://***.com/webhook',
      aes_key_masked: 'dem***ceholder',
      token_masked: 'dem***ceholder',
    }
  }

  // GET /bots/:id/state
  m = _match(url, '/bots/:id/state')
  if (m) {
    const bot = mockBots.find((b) => b.id === parseInt(m.id))
    if (!bot) throw _err(404, 'Bot not found')
    if (botStates[bot.id]) return botStates[bot.id]
    // Build initial state from cluster_configs
    return _buildInitialState(bot)
  }

  // GET /bots/:id/logs
  m = _match(url, '/bots/:id/logs')
  if (m) {
    const bot = mockBots.find((b) => b.id === parseInt(m.id))
    if (!bot) throw _err(404, 'Bot not found')
    const page = parseInt(params.page) || 1
    const limit = parseInt(params.limit) || 50
    const level = params.level || null
    const category = params.category || null

    let entries = mockLogs.filter((l) => l.bot_id === bot.id)
    if (level) entries = entries.filter((l) => l.level.toUpperCase() === level.toUpperCase())
    if (category) entries = entries.filter((l) => l.category === category)
    entries.sort((a, b) => (b.created_at > a.created_at ? 1 : -1))
    const offset = (page - 1) * limit
    return entries.slice(offset, offset + limit)
  }

  // ── Admin ──
  if (url === '/admin/users') {
    const user = _currentUser()
    if (!_isAdmin(user)) throw _err(403, 'Permission denied')
    if (_isSuperAdmin(user)) return [...mockUsers]
    return mockUsers.filter((u) => u.role === 'user' || u.id === user.id)
  }

  if (url === '/admin/bots') {
    const user = _currentUser()
    if (!_isAdmin(user)) throw _err(403, 'Permission denied')
    return mockBots.map((b) => {
      const owner = mockUsers.find((u) => u.id === b.user_id)
      return { ...b, owner: owner ? owner.username : '' }
    })
  }

  if (url === '/admin/backup') {
    // Return a placeholder; in a real deploy this would be a blob
    return { message: 'Demo mode — database backup is not available' }
  }

  if (url === '/admin/stats') {
    return {
      totalUsers: mockUsers.length,
      totalBots: mockBots.length,
      running: mockBots.filter((b) => b.status === 'running').length,
      errors: mockBots.filter((b) => b.status === 'error').length,
    }
  }

  throw _err(404, `GET ${url} not implemented in demo mode`)
}

// ---------------------------------------------------------------------------
// POST handler
// ---------------------------------------------------------------------------

function _handlePost(url, data) {
  // ── Auth ──
  if (url === '/auth/login') {
    // In demo mode any credentials work; find or auto-create the user
    let target = mockUsers.find((u) => u.username === data.username)
    if (!target && data.username) {
      // Auto-create new user on first login attempt
      const newId = Math.max(...mockUsers.map((u) => u.id), 0) + 1
      target = {
        id: newId,
        username: data.username,
        email: `${data.username}@example.com`,
        role: 'user',
        max_running_bots: 5,
        must_change_password: false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }
      mockUsers.push(target)
    }
    if (!target) target = mockUsers[0]
    const tokenVal = target.username === 'demo_user' ? 'demo-token' : String(target.id)
    localStorage.setItem('token', tokenVal)
    return {
      access_token: tokenVal,
      token_type: 'bearer',
      must_change_password: target.must_change_password,
    }
  }

  if (url === '/auth/register') {
    // In demo mode: create user and return a token for auto-login
    const username = data.username || 'new_user'
    const exists = mockUsers.find((u) => u.username === username)
    if (exists) throw _err(409, 'Username already taken')
    const newId = Math.max(...mockUsers.map((u) => u.id), 0) + 1
    const newUser = {
      id: newId,
      username,
      email: data.email || `${username}@example.com`,
      role: 'user',
      max_running_bots: 5,
      must_change_password: false,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }
    mockUsers.push(newUser)
    const token = String(newId)
    localStorage.setItem('token', token)
    return {
      access_token: token,
      token_type: 'bearer',
      must_change_password: false,
    }
  }

  // ── Bots ──
  if (url === '/bots') {
    const user = _currentUser()
    if (!user) throw _err(401, 'Not authenticated')
    const id = nextBotId()
    const bot = {
      id,
      user_id: user.id,
      name: data.name,
      bot_type: (data.bot_type || 'NODE').toUpperCase(),
      platform: data.platform || 'infoflow',
      group_id: data.group_id || null,
      last_user_id: null,
      status: 'stopped',
      last_request_at: null,
      cluster_configs: JSON.stringify(data.cluster_configs || {}),
      config_overrides: JSON.stringify(data.config_overrides || {}),
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }
    mockBots.push(bot)
    appendLog(id, 'system', 'INFO', 'Bot created')
    return bot
  }

  let m
  // POST /bots/:id/start
  m = _match(url, '/bots/:id/start')
  if (m) {
    const bot = mockBots.find((b) => b.id === parseInt(m.id))
    if (!bot) throw _err(404, 'Bot not found')
    if (bot.status === 'running') {
      return { id: bot.id, status: 'running', pid: 12345, consecutive_failures: 0, message: 'Bot is already running' }
    }
    bot.status = 'running'
    bot.updated_at = new Date().toISOString()
    // Initialise state if not present
    if (!botStates[bot.id]) {
      botStates[bot.id] = _buildInitialState(bot)
    }
    appendLog(bot.id, 'system', 'INFO', 'Bot started')
    return { id: bot.id, status: 'running', pid: 12345, consecutive_failures: 0, message: 'Bot started' }
  }

  // POST /bots/:id/stop
  m = _match(url, '/bots/:id/stop')
  if (m) {
    const bot = mockBots.find((b) => b.id === parseInt(m.id))
    if (!bot) throw _err(404, 'Bot not found')
    if (bot.status !== 'running' && bot.status !== 'error') {
      throw _err(409, `Bot is not running (status=${bot.status})`)
    }
    bot.status = 'stopped'
    bot.updated_at = new Date().toISOString()
    appendLog(bot.id, 'system', 'INFO', 'Bot stopped')
    return { id: bot.id, status: 'stopped', pid: null, consecutive_failures: 0, message: 'Bot stopped' }
  }

  // POST /bots/:id/restart
  m = _match(url, '/bots/:id/restart')
  if (m) {
    const bot = mockBots.find((b) => b.id === parseInt(m.id))
    if (!bot) throw _err(404, 'Bot not found')
    bot.status = 'running'
    bot.updated_at = new Date().toISOString()
    if (!botStates[bot.id]) {
      botStates[bot.id] = _buildInitialState(bot)
    }
    appendLog(bot.id, 'system', 'INFO', 'Bot restarted')
    return { id: bot.id, status: 'running', pid: 12346, consecutive_failures: 0, message: 'Bot restarted' }
  }

  // ── Admin ──
  if (url === '/admin/users') {
    const user = _currentUser()
    if (!_isSuperAdmin(user)) throw _err(403, 'Permission denied')
    const exists = mockUsers.find((u) => u.username === data.username)
    if (exists) throw _err(409, 'Username already taken')
    const newId = Math.max(...mockUsers.map((u) => u.id), 0) + 1
    const newUser = {
      id: newId,
      username: data.username,
      email: data.email,
      role: data.role || 'user',
      max_running_bots: data.max_running_bots || 10,
      must_change_password: true,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }
    mockUsers.push(newUser)
    return { id: newUser.id, username: newUser.username, new_password: 'DemoPass123!' }
  }

  // POST /admin/users/:id/reset-password
  m = _match(url, '/admin/users/:id/reset-password')
  if (m) {
    const target = mockUsers.find((u) => u.id === parseInt(m.id))
    if (!target) throw _err(404, 'User not found')
    return { id: target.id, username: target.username, new_password: 'NewDemoPass456!' }
  }

  throw _err(404, `POST ${url} not implemented in demo mode`)
}

// ---------------------------------------------------------------------------
// PUT handler
// ---------------------------------------------------------------------------

function _handlePut(url, data) {
  let m

  // ── Auth ──
  if (url === '/auth/change-password') {
    return { access_token: 'demo-token', token_type: 'bearer', must_change_password: false }
  }

  if (url === '/auth/force-change-password') {
    return { access_token: 'demo-token', token_type: 'bearer', must_change_password: false }
  }

  if (url === '/auth/change-email') {
    const user = _currentUser()
    if (!user) throw _err(401, 'Not authenticated')
    user.email = data.new_email
    return user
  }

  // ── Bots ──
  m = _match(url, '/bots/:id')
  if (m && !url.includes('/state') && !url.includes('/language') && !url.includes('/owner')) {
    const bot = mockBots.find((b) => b.id === parseInt(m.id))
    if (!bot) throw _err(404, 'Bot not found')
    if (bot.status === 'running') throw _err(409, 'Cannot update a running bot. Stop it first.')
    if (data.name !== undefined) bot.name = data.name
    if (data.group_id !== undefined) bot.group_id = data.group_id
    if (data.cluster_configs !== undefined) bot.cluster_configs = JSON.stringify(data.cluster_configs)
    if (data.config_overrides !== undefined) bot.config_overrides = JSON.stringify(data.config_overrides || {})
    bot.updated_at = new Date().toISOString()
    return bot
  }

  // PUT /bots/:id/state
  m = _match(url, '/bots/:id/state')
  if (m) {
    const bot = mockBots.find((b) => b.id === parseInt(m.id))
    if (!bot) throw _err(404, 'Bot not found')
    if (bot.status === 'running') throw _err(409, 'Stop the bot before editing state')
    const { state: alignedState, warnings } = _validateStateForBot(data, bot)
    botStates[bot.id] = alignedState
    const result = { message: 'State updated' }
    if (warnings.length > 0) result.warnings = warnings
    return result
  }

  // PUT /bots/:id/language
  m = _match(url, '/bots/:id/language')
  if (m) {
    const bot = mockBots.find((b) => b.id === parseInt(m.id))
    if (!bot) throw _err(404, 'Bot not found')
    const lang = (data.language || '').toLowerCase()
    if (lang !== 'zh' && lang !== 'en') throw _err(400, "language must be 'zh' or 'en'")
    const overrides = JSON.parse(bot.config_overrides || '{}')
    overrides.LANGUAGE = lang
    bot.config_overrides = JSON.stringify(overrides)
    bot.updated_at = new Date().toISOString()
    appendLog(bot.id, 'system', 'INFO', `Language switched to ${lang}`)
    return { id: bot.id, language: lang }
  }

  // PUT /bots/:id/owner
  m = _match(url, '/bots/:id/owner')
  if (m) {
    const bot = mockBots.find((b) => b.id === parseInt(m.id))
    if (!bot) throw _err(404, 'Bot not found')
    const target = mockUsers.find((u) => u.username === data.username)
    if (!target) throw _err(404, 'Target user not found')
    bot.user_id = target.id
    bot.updated_at = new Date().toISOString()
    appendLog(bot.id, 'system', 'INFO', `Owner transferred to ${data.username}`)
    return { id: bot.id, owner: data.username }
  }

  // ── Admin ──
  m = _match(url, '/admin/users/:id')
  if (m) {
    const user = _currentUser()
    if (!_isAdmin(user)) throw _err(403, 'Permission denied')
    const target = mockUsers.find((u) => u.id === parseInt(m.id))
    if (!target) throw _err(404, 'User not found')
    if (data.username !== undefined) target.username = data.username
    if (data.email !== undefined) target.email = data.email
    if (data.role !== undefined) target.role = data.role
    if (data.max_running_bots !== undefined) target.max_running_bots = data.max_running_bots
    target.updated_at = new Date().toISOString()
    return target
  }

  m = _match(url, '/admin/users/:id/max-bots')
  if (m) {
    const target = mockUsers.find((u) => u.id === parseInt(m.id))
    if (!target) throw _err(404, 'User not found')
    target.max_running_bots = data.max_running_bots
    target.updated_at = new Date().toISOString()
    return { id: target.id, max_running_bots: target.max_running_bots }
  }

  throw _err(404, `PUT ${url} not implemented in demo mode`)
}

// ---------------------------------------------------------------------------
// DELETE handler
// ---------------------------------------------------------------------------

function _handleDelete(url) {
  let m

  m = _match(url, '/bots/:id')
  if (m) {
    const idx = mockBots.findIndex((b) => b.id === parseInt(m.id))
    if (idx === -1) throw _err(404, 'Bot not found')
    const bot = mockBots[idx]
    if (bot.status === 'running') throw _err(409, 'Cannot delete a running bot. Stop it first.')
    mockBots.splice(idx, 1)
    delete botStates[bot.id]
    return
  }

  throw _err(404, `DELETE ${url} not implemented in demo mode`)
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function _parseClusterConfigs(bot) {
  return typeof bot.cluster_configs === 'string' ? JSON.parse(bot.cluster_configs) : bot.cluster_configs || {}
}

function _validateStateForBot(state, bot) {
  return validateBotState(state, bot.bot_type, _parseClusterConfigs(bot))
}

function _buildInitialState(bot) {
  try {
    const cc = typeof bot.cluster_configs === 'string' ? JSON.parse(bot.cluster_configs) : bot.cluster_configs || {}
    if (bot.bot_type === 'DEVICE') {
      const state = {}
      for (const [nodeKey, devices] of Object.entries(cc)) {
        state[nodeKey] = devices.map((model, idx) => ({
          dev_id: idx,
          dev_model: model,
          status: 'idle',
          current_users: [],
        }))
      }
      return state
    }
    // NODE / QUEUE
    const state = {}
    const list = Array.isArray(cc) ? cc : Object.keys(cc)
    for (const name of list) {
      state[name] = { status: 'idle', current_users: [], booking_list: [] }
    }
    return state
  } catch {
    return {}
  }
}

function _err(status, detail) {
  const e = new Error(detail)
  e.response = { status, data: { detail } }
  e.status = status
  return e
}

export { mockApi }
