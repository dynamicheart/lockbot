export function agentActivitySummary(value) {
  const count = value?.recent_active_agents
  const seconds = value?.window_seconds
  if (
    !Number.isSafeInteger(count) ||
    count < 0 ||
    !Number.isSafeInteger(seconds) ||
    seconds <= 0 ||
    seconds > 7 * 86400
  )
    return null
  return { count, minutes: Math.ceil(seconds / 60) }
}

export function credentialAgentActivity(value) {
  const summary = agentActivitySummary(value)
  if (!summary || !Array.isArray(value.items) || value.items.length > 33) return null
  const seen = new Set()
  const items = []
  for (const row of value.items) {
    const label = row?.agent_label
    if (
      label !== null &&
      (typeof label !== 'string' ||
        !label.trim() ||
        [...label].length > 64 ||
        /[\p{C}\p{Zl}\p{Zp}]/u.test(label) ||
        /\b(?:lbr_|lbk_|lba1\.|lbv1\.|Bearer\s+)|\beyJ[\w-]+\.[\w-]+\.[\w-]+/i.test(label))
    )
      continue
    const lastSeen = row?.last_seen_at
    if (
      typeof lastSeen !== 'string' ||
      lastSeen.length > 40 ||
      !/(?:Z|[+-]\d{2}:\d{2})$/.test(lastSeen) ||
      !Number.isFinite(Date.parse(lastSeen)) ||
      seen.has(label)
    )
      continue
    seen.add(label)
    const ip = row.last_observed_ip
    items.push({
      label,
      ip: typeof ip === 'string' && /^[0-9a-f:.]{1,45}$/i.test(ip) ? ip : null,
      lastSeen,
    })
  }
  return { ...summary, items }
}
