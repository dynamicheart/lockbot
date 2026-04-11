/**
 * Demo mode detection.
 *
 * Activates when:
 *  1. VITE_DEMO_MODE env var is 'true' at build time (GitHub Pages), OR
 *  2. ?demo query parameter is present (dev server only, disabled in production).
 *
 * When demo mode is active, the mock API adapter replaces the real
 * axios instance so the frontend works without a backend.
 *
 * Demo and deployment modes are fully isolated via separate localStorage keys.
 */
export const isDemoMode =
  import.meta.env.VITE_DEMO_MODE === 'true' ||
  (import.meta.env.DEV && new URLSearchParams(window.location.search).has('demo'))

// Demo mode uses separate localStorage keys to avoid polluting deploy data.
const _prefix = isDemoMode ? 'lockbot_demo_' : 'lockbot_'
export const LS_KEYS = {
  token: _prefix + 'token',
  user: _prefix + 'user',
  accounts: _prefix + 'accounts',
  locale: 'lockbot_locale',    // shared — UI preference
  theme: 'lockbot_theme',      // shared — UI preference
  mode: 'lockbot_mode',
}

// One-time migration: copy old demo data (demo: tokens only) to new prefixed keys.
// Never copy deploy (real JWT) data to prevent cross-mode pollution.
if (isDemoMode) {
  // Migrate token/user only if the old token is a demo token
  const oldToken = localStorage.getItem('lockbot_token') || ''
  if (oldToken.startsWith('demo:') && !localStorage.getItem(LS_KEYS.token)) {
    localStorage.setItem(LS_KEYS.token, oldToken)
    localStorage.setItem(LS_KEYS.user, localStorage.getItem('lockbot_user') || 'null')
  }
  // Migrate accounts, keeping only demo: entries
  if (!localStorage.getItem(LS_KEYS.accounts)) {
    try {
      const old = JSON.parse(localStorage.getItem('lockbot_accounts') || '[]')
      const demoOnly = old.filter(a => a.token?.startsWith('demo:'))
      if (demoOnly.length > 0) {
        localStorage.setItem(LS_KEYS.accounts, JSON.stringify(demoOnly))
      }
    } catch { /* ignore corrupt data */ }
  }
}

/**
 * Seed saved accounts for pre-installed demo users so they appear
 * in the login page's account switcher. Merges with existing accounts.
 */
if (isDemoMode) {
  const existing = JSON.parse(localStorage.getItem(LS_KEYS.accounts) || '[]')
  const existingNames = new Set(existing.map(a => a.user?.username))
  const seedUsers = [
    { id: 1, username: 'demo_user', email: 'demo@example.com', role: 'super_admin', max_running_bots: 10 },
    { id: 2, username: 'admin', email: 'admin@example.com', role: 'admin', max_running_bots: 10 },
    { id: 3, username: 'user1', email: 'user1@example.com', role: 'user', max_running_bots: 5 },
    { id: 4, username: 'user2', email: 'user2@example.com', role: 'user', max_running_bots: 3 },
    { id: 5, username: 'researcher', email: 'researcher@example.com', role: 'admin', max_running_bots: 8 },
    { id: 6, username: 'intern', email: 'intern@example.com', role: 'user', max_running_bots: 2 },
  ]
  let changed = false
  for (const u of seedUsers) {
    if (!existingNames.has(u.username)) {
      existing.push({ token: 'demo:' + String(u.id), user: u })
      changed = true
    }
  }
  if (changed) {
    localStorage.setItem(LS_KEYS.accounts, JSON.stringify(existing))
  }
}
