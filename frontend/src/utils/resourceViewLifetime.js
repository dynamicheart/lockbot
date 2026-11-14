// A null server expiry explicitly means permanent. Missing/malformed expiry is not permanent.
export function resourceViewDeadline(expiresAt, serverOffset = 0) {
  if (expiresAt === null) return 0
  if (typeof expiresAt !== 'string') return null
  const expiry = Date.parse(expiresAt)
  return Number.isFinite(expiry) && Number.isFinite(serverOffset) ? expiry - serverOffset : null
}
