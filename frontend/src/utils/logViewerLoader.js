/** Scope legacy log reads and downloads to the bot and filters that requested them. */
export function createLogViewerLoader({ request, publish, save }) {
  let scope = null
  let scopeKey
  let generation = 0
  let reader = null
  let downloader = null
  let disposed = false
  let state = emptyState()

  function emptyState() {
    return {
      logs: [],
      page: 1,
      hasMore: false,
      loading: false,
      loadingMore: false,
      refreshing: false,
      downloading: false,
    }
  }

  function update(changes) {
    state = { ...state, ...changes }
    publish(changes)
  }

  function params(target, extra) {
    return {
      ...extra,
      include_private: !!target.includePrivate,
      ...(target.level ? { level: target.level } : {}),
      ...(target.category ? { category: target.category } : {}),
    }
  }

  async function read(mode = 'initial') {
    if (disposed || reader || !scope || scope.category === 'agent') return false
    if (mode === 'more' && !state.hasMore) return false
    if (mode === 'newer' && !state.logs[0]?.created_at) mode = 'initial'
    const current = generation
    const target = { ...scope }
    const active = new AbortController()
    reader = active
    const page = mode === 'more' ? state.page + 1 : 1
    const query = params(target, {
      page,
      limit: 50,
      ...(mode === 'newer' ? { since: state.logs[0].created_at } : {}),
    })
    update({
      loading: mode === 'initial',
      loadingMore: mode === 'more',
      refreshing: mode === 'newer',
    })
    try {
      const rows = await request(target.botId, query, active.signal)
      if (disposed || current !== generation) return false
      if (mode === 'newer') {
        const existing = new Set(state.logs.map((row) => row.id))
        update({
          logs: [...rows.filter((row) => row.id == null || !existing.has(row.id)), ...state.logs],
        })
      } else {
        update({
          logs: mode === 'more' ? [...state.logs, ...rows] : rows,
          page,
          hasMore: rows.length === 50,
        })
      }
      return true
    } catch {
      // The HTTP interceptor reports failures. A failed page is never committed.
      return false
    } finally {
      if (!disposed && current === generation) {
        reader = null
        update({ loading: false, loadingMore: false, refreshing: false })
      }
    }
  }

  function setScope(next) {
    if (disposed) return Promise.resolve(false)
    const key = JSON.stringify(next)
    if (key !== scopeKey) {
      generation++
      reader?.abort()
      downloader?.abort()
      reader = downloader = null
      scope = { ...next }
      scopeKey = key
      state = emptyState()
      publish(state)
    }
    return read()
  }

  async function download() {
    if (disposed || downloader || !scope || scope.category === 'agent') return false
    const current = generation
    const target = { ...scope }
    const active = new AbortController()
    downloader = active
    update({ downloading: true })
    try {
      const rows = await request(
        target.botId,
        params(target, { page: 1, limit: 10000 }),
        active.signal
      )
      if (disposed || current !== generation) return false
      save({ rows, filename: `bot_${target.botId}_${target.category || 'all'}_logs.txt` })
      return true
    } catch {
      return false
    } finally {
      if (!disposed && current === generation) {
        downloader = null
        update({ downloading: false })
      }
    }
  }

  function dispose() {
    disposed = true
    generation++
    reader?.abort()
    downloader?.abort()
    reader = downloader = null
  }

  return { setScope, refresh: () => read('newer'), loadMore: () => read('more'), download, dispose }
}
