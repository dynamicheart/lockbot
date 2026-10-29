// Lock ownership is deliberately separate from telemetry load.
export function loadLevel(cards, now, staleSeconds, status) {
  if (
    !cards.length ||
    cards.some((card) =>
      Object.values(card.metrics).some((metric) => status(metric, now, staleSeconds) !== 'ok')
    )
  )
    return 'unknown'
  const peak = Math.max(...cards.map((card) => card.metrics.compute.value))
  return peak >= 80 ? 'high' : peak >= 30 ? 'medium' : 'low'
}

export function sourceResources(cluster, botType) {
  const rows = Array.isArray(cluster)
    ? cluster.map((row) =>
        typeof row === 'string' ? [row, []] : [row?.node_key, row?.devices || []]
      )
    : cluster && typeof cluster === 'object'
      ? Object.entries(cluster)
      : []
  return {
    nodes: rows.map(([node]) => node).filter((node) => typeof node === 'string' && node.length),
    resources: ['DEVICE', 'QUEUE_DEVICE'].includes(botType)
      ? rows.flatMap(([node, devices]) =>
          typeof node === 'string' && node && Array.isArray(devices)
            ? devices.map((_, index) => `${node}:dev${index}`)
            : []
        )
      : [],
  }
}

// A successful request is not proof that every configured resource has fresh data.
export function sourceHealth(snapshot, config, nodes, now, status) {
  const issues = []
  const times = []
  for (const node of nodes) {
    const target = config?.nodeMapping?.[node]
    const cards = snapshot?.cards?.filter((card) => card.node === target) || []
    const host = snapshot?.hosts?.find((item) => item.node === target)
    const metrics = cards.flatMap((card) => Object.values(card.metrics))
    if (config?.profile === 'lockbot-v1' && host) {
      metrics.push(
        host.cpu,
        host.used,
        host.total,
        ...host.filesystems.flatMap((d) => [d.used, d.total])
      )
      for (const key of ['available', 'cache']) if (host[key]?.seen) metrics.push(host[key])
    }
    const states = metrics.map((metric) => status(metric, now, config?.staleSeconds || 90))
    if (!target || !cards.length || (config?.profile === 'lockbot-v1' && !host))
      issues.push({ node, state: 'missing' })
    else if (states.some((state) => state !== 'ok'))
      issues.push({
        node,
        state: states.includes('failed')
          ? 'failed'
          : states.includes('stale')
            ? 'stale'
            : 'invalid',
      })
    for (const metric of metrics)
      if (Number.isFinite(metric?.observedAt)) times.push(metric.observedAt)
  }
  return { issues, sampledAt: times.length ? Math.min(...times) : null }
}
