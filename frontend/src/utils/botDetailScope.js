/** Local detail-page identity; revisiting the same ID still creates a new generation. */
export function createBotDetailScope() {
  let id = ''
  let generation = 0
  let disposed = false
  const channels = new Map()
  function cancel(channel) {
    channels.get(channel)?.abort()
    channels.delete(channel)
  }
  function capture() {
    const current = generation
    return { id, isCurrent: () => !disposed && generation === current }
  }
  function enter(nextId) {
    generation++
    for (const channel of channels.keys()) cancel(channel)
    id = String(nextId || '')
  }
  function begin(channel) {
    if (disposed || !id) return null
    cancel(channel)
    const target = capture()
    const controller = new AbortController()
    channels.set(channel, controller)
    return {
      id: target.id,
      signal: controller.signal,
      isCurrent: () => target.isCurrent() && channels.get(channel) === controller,
      finish: () => {
        if (channels.get(channel) === controller) channels.delete(channel)
      },
    }
  }
  function dispose() {
    enter('')
    disposed = true
  }
  return { enter, capture, begin, cancel, dispose }
}
