import { expect, it } from 'vitest'
import { trafficBarReference } from './trafficBarReference'

it.each([0, 1, 25, 100])('anchors percentage %s to the bottom value envelope', (percent) => {
  const element = {
    dataset: { barHeight: String(percent) },
    getBoundingClientRect: () => ({ left: 20, right: 30, bottom: 200, width: 10, height: 160 }),
  }
  const reference = trafficBarReference(element)
  const height = Math.max(2, (160 * percent) / 100)
  expect(reference.contextElement).toBe(element)
  expect(reference.getBoundingClientRect()).toEqual({
    x: 20,
    y: 200 - height,
    top: 200 - height,
    bottom: 200,
    left: 20,
    right: 30,
    width: 10,
    height,
  })
})

it('reads current geometry after scroll or resize instead of retaining stale layout', () => {
  let bottom = 200,
    height = 100
  const reference = trafficBarReference({
    dataset: { barHeight: '50' },
    getBoundingClientRect: () => ({ left: 0, right: 10, bottom, width: 10, height }),
  })
  expect(reference.getBoundingClientRect().top).toBe(150)
  bottom = 90
  height = 60
  expect(reference.getBoundingClientRect()).toMatchObject({ top: 60, height: 30 })
})
