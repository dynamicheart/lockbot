export function formatNodeAccess({ host, ip, port }) {
  const primary = `${host.includes(':') ? `[${host}]` : host}:${port}`
  return ip ? `${primary} · ${ip}:${port}` : primary
}
