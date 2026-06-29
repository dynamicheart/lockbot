import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'

/**
 * Check if cluster_configs is in the DEVICE array format [{node_key, devices}].
 */
export function isDeviceArrayFormat(cc) {
  return Array.isArray(cc) && cc.length > 0 && cc[0]?.node_key !== undefined
}

/**
 * Extract ordered node keys from any cluster_configs format.
 * - DEVICE array: [{node_key, devices}] → [node_key, ...]
 * - NODE/QUEUE array: ["n1", "n2"] → ["n1", "n2"]
 * - Legacy DEVICE dict: {key: [...]} → Object.keys(...)
 */
export function getNodeOrder(cc) {
  if (!cc) return []
  if (isDeviceArrayFormat(cc)) return cc.map((item) => item.node_key)
  if (Array.isArray(cc)) return cc
  return Object.keys(cc)
}

export function botTypeTagClass(type) {
  const map = { NODE: 'bot-type-node', DEVICE: 'bot-type-device', QUEUE: 'bot-type-queue' }
  return map[type] || 'bot-type-default'
}

/**
 * Return Object.entries(data) ordered by cluster_configs key order.
 * Handles both array configs (NODE/QUEUE) and dict configs (DEVICE).
 * Falls back to original Object.entries order for keys not in config.
 *
 * Why: JS objects auto-sort pure-integer keys (e.g. "0","1","2") by numeric value,
 * ignoring insertion order. This function enforces the intended order from cluster_configs.
 */
export function orderedEntries(data, clusterConfigs) {
  if (!data || !clusterConfigs) return Object.entries(data || {})
  const order = getNodeOrder(clusterConfigs)
  const dataKeys = new Map(Object.keys(data).map((k) => [k.toLowerCase(), k]))
  const result = []
  for (const key of order) {
    const actual = dataKeys.get(key.toLowerCase())
    if (actual !== undefined) {
      result.push([actual, data[actual]])
      dataKeys.delete(key.toLowerCase())
    }
  }
  for (const actual of dataKeys.values()) {
    result.push([actual, data[actual]])
  }
  return result
}

/**
 * JSON.stringify that respects cluster_configs key order.
 * Standard JSON.stringify always sorts numeric keys; this preserves the intended order.
 */
export function orderedStringify(data, clusterConfigs, indent = 2) {
  if (!data || !clusterConfigs) return JSON.stringify(data, null, indent)
  const entries = orderedEntries(data, clusterConfigs)
  const sp = ' '.repeat(indent)
  const inner = entries
    .map(
      ([k, v]) =>
        `${sp}${JSON.stringify(k)}: ${JSON.stringify(v, null, indent).replace(/\n/g, '\n' + sp)}`
    )
    .join(',\n')
  return `{\n${inner}\n}`
}

/**
 * Composable providing shared utility functions with i18n support.
 */
export function useHelpers() {
  const { t } = useI18n()

  /** Parse a datetime string, treating naive strings as UTC. */
  function _parseDate(d) {
    if (typeof d === 'string' && !d.endsWith('Z') && !d.includes('+')) {
      d = d + 'Z'
    }
    return new Date(d)
  }

  function formatDate(d) {
    if (!d) return '-'
    return _parseDate(d).toLocaleDateString()
  }

  function formatDateTime(d) {
    if (!d) return '-'
    return _parseDate(d).toLocaleString()
  }

  function formatRelativeTime(d) {
    if (!d) return ''
    const diff = Date.now() - _parseDate(d).getTime()
    const sec = Math.floor(diff / 1000)
    if (sec < 60) return t('botDetail.justNow')
    const min = Math.floor(sec / 60)
    if (min < 60) return t('botDetail.minutesAgo', { n: min })
    const hr = Math.floor(min / 60)
    if (hr < 24) return t('botDetail.hoursAgo', { n: hr })
    const day = Math.floor(hr / 24)
    if (day < 30) return t('botDetail.daysAgo', { n: day })
    return formatDate(d)
  }

  function maskText(text) {
    if (!text) return '-'
    if (text.length <= 4) return '****'
    return '*'.repeat(text.length - 4) + text.slice(-4)
  }

  function copyText(text, successMsg) {
    if (!text) return
    const msg = successMsg || t('common.copied')
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        () => ElMessage.success(msg),
        () => fallbackCopy(text, msg)
      )
    } else {
      fallbackCopy(text, msg)
    }
  }

  function fallbackCopy(text, msg) {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.cssText = 'position:fixed;left:-9999px'
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
      ElMessage.success(msg || t('common.copied'))
    } catch {
      ElMessage.warning(t('common.copyFailed'))
    }
    document.body.removeChild(ta)
  }

  return {
    formatDate,
    formatDateTime,
    formatRelativeTime,
    maskText,
    copyText,
    fallbackCopy,
  }
}
