// Display policy only. Epoch values, freshness checks and lease arithmetic remain unchanged.
const formatters = new Map()
const naiveServerTime = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/

export function formatResourceTime(value, locale = 'zh', preferredTimeZone) {
  if (value == null || value === '') return '—'
  // Backend naive datetimes represent UTC, as in the existing shared date helpers.
  const input =
    typeof value === 'string' && naiveServerTime.test(value) ? value.replace(' ', 'T') + 'Z' : value
  const date = value instanceof Date ? value : new Date(input)
  if (!Number.isFinite(date.getTime())) return '—'
  // Language does not identify a timezone. An explicit preference takes priority;
  // otherwise resolve the viewer's current browser zone, not the API server's zone.
  const timeZone = preferredTimeZone ?? new Intl.DateTimeFormat(locale).resolvedOptions().timeZone
  try {
    if (!formatters.has(timeZone)) {
      formatters.set(
        timeZone,
        new Intl.DateTimeFormat('en-GB', {
          timeZone,
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hourCycle: 'h23',
          timeZoneName: 'shortOffset',
        })
      )
    }
  } catch {
    return '—'
  }
  const parts = Object.fromEntries(
    formatters
      .get(timeZone)
      .formatToParts(date)
      .map(({ type, value }) => [type, value])
  )
  const offset = parts.timeZoneName.match(/^GMT([+-])(\d{1,2})(?::(\d{2}))?$/)
  const zoneLabel = offset && (Number(offset[2]) !== 0 || Number(offset[3] || 0) !== 0)
    ? `UTC${offset[1]}${offset[2].padStart(2, '0')}:${offset[3] || '00'}`
    : 'UTC'
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} ${zoneLabel}`
}
