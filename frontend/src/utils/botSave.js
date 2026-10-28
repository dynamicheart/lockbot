function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical)
  if (value && typeof value === 'object')
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonical(value[key])])
    )
  return value
}

// Compare normalized form snapshots, preserving meaningful resource array ordering.
// Authorization is saved separately in the same payload and never triggers restart.
export function changedConfig(current, baseline) {
  if (!baseline) return {}
  return Object.fromEntries(
    Object.entries(current).filter(
      ([key, value]) =>
        JSON.stringify(canonical(value)) !== JSON.stringify(canonical(baseline[key]))
    )
  )
}
