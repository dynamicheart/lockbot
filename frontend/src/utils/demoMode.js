/**
 * Demo mode detection.
 *
 * Activates when:
 *  1. VITE_DEMO_MODE env var is 'true' at build time, OR
 *  2. ?demo query parameter is present in the URL at runtime.
 *
 * When demo mode is active, the mock API adapter replaces the real
 * axios instance so the frontend works without a backend.
 */
export const isDemoMode =
  import.meta.env.VITE_DEMO_MODE === 'true' ||
  new URLSearchParams(window.location.search).has('demo')

/**
 * Seed saved accounts for pre-installed demo users so they appear
 * in the login page's account switcher.
 */
if (isDemoMode) {
  const ACCOUNTS_KEY = 'lockbot_accounts'
  const existing = JSON.parse(localStorage.getItem(ACCOUNTS_KEY) || '[]')
  if (existing.length === 0) {
    const seedUsers = [
      { id: 1, username: 'demo_user', email: 'demo@example.com', role: 'super_admin', max_running_bots: 10 },
      { id: 2, username: 'admin', email: 'admin@example.com', role: 'admin', max_running_bots: 10 },
      { id: 3, username: 'user1', email: 'user1@example.com', role: 'user', max_running_bots: 5 },
      { id: 4, username: 'user2', email: 'user2@example.com', role: 'user', max_running_bots: 3 },
      { id: 5, username: 'researcher', email: 'researcher@example.com', role: 'admin', max_running_bots: 8 },
      { id: 6, username: 'intern', email: 'intern@example.com', role: 'user', max_running_bots: 2 },
    ]
    const seeded = seedUsers.map(u => ({ token: String(u.id), user: u }))
    localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(seeded))
  }
}
