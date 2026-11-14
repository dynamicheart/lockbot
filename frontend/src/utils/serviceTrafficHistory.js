import { createServiceTrafficLoader } from './serviceTrafficLoader'

export const historyPresets = ['24h', '7d', '30d', 'custom']
export function historyRange(preset, custom = [], now = Date.now()) {
  const days = { '24h': 1, '7d': 7, '30d': 30 }
  const start = preset === 'custom' ? new Date(custom[0]).getTime() : now - days[preset] * 86400000
  const end = preset === 'custom' ? new Date(custom[1]).getTime() : now
  if (
    !Number.isFinite(start) ||
    !Number.isFinite(end) ||
    start >= end ||
    end - start > 31 * 86400000 ||
    end > now + 60000
  )
    return null
  const seconds = (end - start) / 1000
  return {
    start: new Date(start).toISOString(),
    end: new Date(end).toISOString(),
    resolution_seconds: seconds <= 86400 ? 900 : seconds <= 7 * 86400 ? 3600 : 21600,
  }
}

export function historyEndpoint(botId, admin) {
  return admin ? '/admin/service-traffic/history' : `/bots/${botId}/service-traffic/history`
}
export function createTrafficHistoryLoader({ request, publish }) {
  let enabled = true
  const loader = createServiceTrafficLoader({ request, publish, canRequest: () => enabled })
  return {
    ...loader,
    setScope(next) {
      enabled = !!next
      return loader.setScope(next)
    },
  }
}
export function historyRows(snapshot) {
  return (snapshot?.history || []).map((point) => ({
    ...point,
    known:
      ['complete', 'partial'].includes(point.coverage) &&
      point.observed === true &&
      Number.isFinite(point.requests),
  }))
}
export function operationCount(point) {
  return ['chat', 'agent', 'operator'].reduce(
    (sum, source) => sum + (Number(point.operations?.[source]) || 0),
    0
  )
}
