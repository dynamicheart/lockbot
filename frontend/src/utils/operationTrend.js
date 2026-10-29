export const operationSources = ['chat', 'agent', 'operator']

// Align only actual minute evidence to the HTTP axis; never spread cumulative rows.
export function buildOperationTrend(snapshot) {
  const evidence = new Map(
    (snapshot?.business_operations?.history || []).map((point) => [point.at, point])
  )
  const totals = Object.fromEntries(operationSources.map((source) => [source, 0]))
  const history = (snapshot?.history || []).map(({ at }) => {
    const point = evidence.get(at)
    const observed = point?.observed === true
    const sources = Object.fromEntries(
      operationSources.map((source) => {
        const raw = point?.sources?.[source]
        const count = observed && Number.isSafeInteger(raw) && raw >= 0 ? raw : 0
        totals[source] += count
        return [source, count]
      })
    )
    return { at, observed, sources, total: Object.values(sources).reduce((a, b) => a + b, 0) }
  })
  const max = Math.max(1, ...history.map((point) => point.total))
  for (const point of history) {
    let bottom = 0
    point.segments = operationSources.map((source) => {
      const height = (point.sources[source] / max) * 100
      const segment = { source, count: point.sources[source], height, bottom }
      bottom += height
      return segment
    })
  }
  return { history, totals, max, hasActivity: history.some((point) => point.total > 0) }
}
