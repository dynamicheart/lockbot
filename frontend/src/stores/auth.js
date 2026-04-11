import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../utils/api'
import { LS_KEYS } from '../utils/demoMode'

function loadAccounts() {
  try { return JSON.parse(localStorage.getItem(LS_KEYS.accounts) || '[]') } catch { return [] }
}

function saveAccounts(accounts) {
  localStorage.setItem(LS_KEYS.accounts, JSON.stringify(accounts))
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem(LS_KEYS.token) || '')
  const user = ref(JSON.parse(localStorage.getItem(LS_KEYS.user) || 'null'))

  // Auto-save current session to accounts list on init
  if (token.value && user.value) {
    saveAccount({ token: token.value, user: user.value })
  }

  const isLoggedIn = computed(() => !!token.value)
  const isAdmin = computed(() => user.value?.role === 'admin' || user.value?.role === 'super_admin')
  const isSuperAdmin = computed(() => user.value?.role === 'super_admin')

  // --- Multi-account: get all saved accounts ---
  function getSavedAccounts() {
    return loadAccounts()
  }

  // --- Multi-account: login and save to account list ---
  async function login(username, password) {
    const res = await api.post('/auth/login', { username, password })
    token.value = res.data.access_token
    localStorage.setItem(LS_KEYS.token, token.value)
    await fetchUser()
    // Save to account list (upsert by username)
    saveAccount({ token: token.value, user: user.value })
    return res.data.must_change_password
  }

  // --- Multi-account: switch to another account ---
  async function switchAccount(username) {
    const accounts = loadAccounts()
    const account = accounts.find(a => a.user?.username === username)
    if (!account) return false
    token.value = account.token
    localStorage.setItem(LS_KEYS.token, token.value)
    try {
      // Verify token is still valid by fetching user
      const res = await api.get('/auth/me')
      user.value = res.data
      localStorage.setItem(LS_KEYS.user, JSON.stringify(user.value))
      // Update saved token in case it was refreshed
      account.token = token.value
      account.user = user.value
      saveAccounts(accounts)
      return true
    } catch {
      // Token expired, remove this account
      removeAccount(username)
      return false
    }
  }

  // --- Multi-account: remove account from list ---
  function removeAccount(username) {
    const accounts = loadAccounts().filter(a => a.user?.username !== username)
    saveAccounts(accounts)
  }

  // --- Multi-account: save current session ---
  function saveAccount(data) {
    const accounts = loadAccounts()
    const idx = accounts.findIndex(a => a.user?.username === data.user?.username)
    if (idx >= 0) {
      accounts[idx] = data
    } else {
      accounts.push(data)
    }
    saveAccounts(accounts)
  }

  // --- Multi-account: update current account in list ---
  function updateCurrentAccount() {
    if (!user.value) return
    const accounts = loadAccounts()
    const idx = accounts.findIndex(a => a.user?.username === user.value.username)
    if (idx >= 0) {
      accounts[idx] = { token: token.value, user: user.value }
      saveAccounts(accounts)
    }
  }

  async function register(username, email, password) {
    await api.post('/auth/register', { username, email, password })
  }

  async function fetchUser() {
    const res = await api.get('/auth/me')
    user.value = res.data
    localStorage.setItem(LS_KEYS.user, JSON.stringify(user.value))
  }

  async function changePassword(currentPassword, newPassword) {
    const res = await api.put('/auth/change-password', {
      current_password: currentPassword,
      new_password: newPassword,
    })
    token.value = res.data.access_token
    localStorage.setItem(LS_KEYS.token, token.value)
    await fetchUser()
    updateCurrentAccount()
  }

  async function changeEmail(newEmail) {
    const res = await api.put('/auth/change-email', { new_email: newEmail })
    user.value = res.data
    localStorage.setItem(LS_KEYS.user, JSON.stringify(user.value))
    updateCurrentAccount()
  }

  function logout() {
    // Remove current account from saved list
    if (user.value) removeAccount(user.value.username)
    token.value = ''
    user.value = null
    localStorage.removeItem(LS_KEYS.token)
    localStorage.removeItem(LS_KEYS.user)
  }

  return {
    token, user, isLoggedIn, isAdmin, isSuperAdmin,
    login, register, fetchUser, changePassword, changeEmail, logout,
    getSavedAccounts, switchAccount, removeAccount, saveAccount,
  }
})
