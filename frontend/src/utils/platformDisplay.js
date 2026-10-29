/** Display labels only; protocol and persisted platform values remain unchanged. */
export function platformLabel(value, t) {
  const normalized = String(value || '').toLowerCase()
  if (normalized === 'infoflow') return t('feishu.infoflowLabel')
  if (normalized === 'feishu') return t('feishu.feishuLabel')
  return value || '—'
}
