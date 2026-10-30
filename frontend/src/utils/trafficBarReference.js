// Keep a generous full-column hit target, but anchor the tooltip to its value.
// Read geometry on demand so scrolling/resizing cannot leave a stale tooltip box.
export function trafficBarReference(element) {
  return {
    contextElement: element,
    getBoundingClientRect() {
      const rect = element.getBoundingClientRect()
      const raw = Number(element.dataset.barHeight)
      const fraction = Number.isFinite(raw) ? Math.max(0, Math.min(100, raw)) / 100 : 0
      const height = Math.min(rect.height, Math.max(2, rect.height * fraction))
      const top = rect.bottom - height
      return {
        x: rect.left,
        y: top,
        top,
        bottom: rect.bottom,
        left: rect.left,
        right: rect.right,
        width: rect.width,
        height,
      }
    },
  }
}
