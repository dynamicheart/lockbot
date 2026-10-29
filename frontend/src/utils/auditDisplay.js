const dispatchNote =
  'dispatched; this is not an assertion that the operation took effect. Read GET /api/bots/{id}/resources for authoritative state.'

export function formatAuditDetail(detail, { fields, sources, booleans, dispatchedNote }) {
  let value = detail
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value)
    } catch {
      return value
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return JSON.stringify(value)
  return Object.entries(value)
    .map(([key, raw]) => {
      let text = raw
      if (key === 'source') text = sources?.[raw] ?? raw
      else if (key === 'note' && raw === dispatchNote) text = dispatchedNote
      else if (typeof raw === 'boolean') text = booleans?.[String(raw)] ?? String(raw)
      else if (raw && typeof raw === 'object' && !Array.isArray(raw) && 'to' in raw) {
        const display = (v) =>
          typeof v === 'boolean' ? (booleans?.[String(v)] ?? String(v)) : String(v)
        text = 'from' in raw ? `${display(raw.from)} → ${display(raw.to)}` : display(raw.to)
      } else if (typeof raw === 'object') text = JSON.stringify(raw)
      return `${fields?.[key] ?? key}: ${text}`
    })
    .join('\n')
}

/** Render retained rows without rewriting historical audit evidence. */
export function auditTarget(row) {
  const globalRead =
    row?.action === 'task_private.read' &&
    row?.target_id == null &&
    row?.detail?.surface === 'management.global_service_traffic'
  return {
    type: globalRead ? 'service' : row?.target_type,
    name:
      row?.target_name ||
      (globalRead ? 'Lockbot' : row?.target_id != null ? `#${row.target_id}` : ''),
  }
}
