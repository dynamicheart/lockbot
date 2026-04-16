import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../utils/api'

export const useBotsStore = defineStore('bots', () => {
  const bots = ref([])
  const loading = ref(false)
  const runningStates = ref({})

  async function fetchBots() {
    loading.value = true
    try {
      const res = await api.get('/bots')
      bots.value = res.data
    } catch {
      // handled by interceptor
    } finally {
      loading.value = false
    }
  }

  async function fetchRunningStates() {
    try {
      const res = await api.get('/bots/running-states')
      runningStates.value = res.data
    } catch {
      runningStates.value = {}
    }
  }

  function parseClusterConfigs(configsStr) {
    try {
      return typeof configsStr === 'string' ? JSON.parse(configsStr) : configsStr || {}
    } catch {
      return {}
    }
  }

  function computeBotStats(bot) {
    const configs = parseClusterConfigs(bot.cluster_configs)
    const state = runningStates.value[String(bot.id)]

    // Resource counts from cluster_configs
    let resourceCounts
    if (bot.bot_type === 'DEVICE') {
      let totalDevices = 0
      for (const devices of Object.values(configs)) {
        totalDevices += Array.isArray(devices) ? devices.length : 0
      }
      resourceCounts = { nodes: Object.keys(configs).length, devices: totalDevices }
    } else {
      resourceCounts = { nodes: Object.keys(configs).length }
    }

    // Utilization from live state (only for running bots)
    let utilization = null
    if (state) {
      const values = Object.values(state)
      if (values.length > 0) {
        if (bot.bot_type === 'DEVICE') {
          let total = 0,
            inUse = 0
          for (const devices of values) {
            if (Array.isArray(devices)) {
              for (const d of devices) {
                total++
                if (d.status !== 'idle') inUse++
              }
            }
          }
          if (total > 0) utilization = { total, inUse, idle: total - inUse }
        } else {
          const total = values.length
          const inUse = values.filter((n) => n && n.status !== 'idle').length
          utilization = { total, inUse, idle: total - inUse }
        }
      }
    }

    return { resourceCounts, utilization }
  }

  async function createBot(data) {
    const res = await api.post('/bots', data)
    return res.data
  }

  async function getBot(id) {
    const res = await api.get(`/bots/${id}`)
    return res.data
  }

  async function updateBot(id, data) {
    const res = await api.put(`/bots/${id}`, data)
    return res.data
  }

  async function deleteBot(id) {
    await api.delete(`/bots/${id}`)
  }

  async function startBot(id) {
    const res = await api.post(`/bots/${id}/start`)
    return res.data
  }

  async function stopBot(id) {
    const res = await api.post(`/bots/${id}/stop`)
    return res.data
  }

  async function restartBot(id) {
    const res = await api.post(`/bots/${id}/restart`)
    return res.data
  }

  async function getBotState(id) {
    const res = await api.get(`/bots/${id}/state`)
    return res.data
  }

  async function updateBotState(id, state) {
    const res = await api.put(`/bots/${id}/state`, state)
    return res.data
  }

  async function transferOwner(id, username) {
    const res = await api.put(`/bots/${id}/owner`, { username })
    return res.data
  }

  async function getBotLogs(id, params = {}) {
    const res = await api.get(`/bots/${id}/logs`, { params })
    return res.data
  }

  async function setBotLanguage(id, language) {
    const res = await api.put(`/bots/${id}/language`, { language })
    return res.data
  }

  return {
    bots,
    loading,
    runningStates,
    fetchBots,
    fetchRunningStates,
    computeBotStats,
    parseClusterConfigs,
    createBot,
    getBot,
    updateBot,
    deleteBot,
    startBot,
    stopBot,
    restartBot,
    getBotState,
    updateBotState,
    transferOwner,
    getBotLogs,
    setBotLanguage,
  }
})
