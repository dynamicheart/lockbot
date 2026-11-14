// Display policy only. Epoch values, freshness checks and lease arithmetic remain unchanged.
const formatters = new Map()
const naiveServerTime = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/

export function formatResourceTime(value, locale = 'zh') {
  if (value == null || value === '') return '—'
  // Backend naive datetimes represent UTC, as in the existing shared date helpers.
  const input =
    typeof value === 'string' && naiveServerTime.test(value) ? value.replace(' ', 'T') + 'Z' : value
  const date = value instanceof Date ? value : new Date(input)
  if (!Number.isFinite(date.getTime())) return '—'
  const chinese = /^zh(?:[-_]|$)/i.test(locale)
  const timeZone = chinese ? 'Asia/Shanghai' : 'UTC'
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
      })
    )
  }
  const parts = Object.fromEntries(
    formatters
      .get(timeZone)
      .formatToParts(date)
      .map(({ type, value }) => [type, value])
  )
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} ${chinese ? 'UTC+08:00' : 'UTC'}`
}
