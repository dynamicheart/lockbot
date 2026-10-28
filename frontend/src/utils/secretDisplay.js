// All secret fields share the same visual treatment, including webhook URLs.
export function maskSecret(value) {
  if (!value) return '-'
  if (value.length <= 4) return '****'
  return '*'.repeat(value.length - 4) + value.slice(-4)
}
