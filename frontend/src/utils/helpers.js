import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'

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
        () => fallbackCopy(text, msg),
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
