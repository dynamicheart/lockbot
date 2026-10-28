/** Backend naive timestamps are UTC; respect explicit offsets and the UI locale. */
export function formatActivityTime(value, locale, timeZone) {
  if (typeof value !== 'string' || !value.trim()) return '—'
  let iso = value.trim().replace(/(\.\d{3})\d+/, '$1')
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso)) iso += 'Z'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat(locale, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
    ...(timeZone ? { timeZone } : {}),
  }).format(date)
}
