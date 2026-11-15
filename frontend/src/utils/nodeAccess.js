export function formatNodeAccess({ host, ipv4, port }) {
  const primary = `${host.includes(':') ? `[${host}]` : host}:${port}`
  return ipv4 ? `${primary} · ${ipv4}:${port}` : primary
}
