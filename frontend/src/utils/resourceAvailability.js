// An idle QUEUE resource still belongs to its first booker's claim window.
export function resourceStatus(resource) {
  const status = resource?.lock_status
  if (!['idle', 'exclusive', 'shared'].includes(status)) return 'unknown'
  return status === 'idle' && resource.booking_list?.length ? 'pending' : status
}
export const isIdleResource = (resource) => resourceStatus(resource) === 'idle'
