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
