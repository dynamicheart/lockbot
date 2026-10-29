import { describe, it, expect } from 'vitest'
import { buildOperationTrend } from './operationTrend'

const axis = [
  { at: 'a', requests: 99 },
  { at: 'b', requests: 2 },
  { at: 'c', requests: 5 },
]

describe('operation trend evidence', () => {
  it('aligns minute evidence to the HTTP axis and stacks sources without mixing request totals', () => {
    const result = buildOperationTrend({
      history: axis,
      business_operations: {
        history: [
          { at: 'b', observed: true, sources: { chat: 2, agent: 3, operator: 1 } },
          { at: 'c', observed: true, sources: { chat: 1, agent: 0, operator: 0 } },
        ],
      },
    })
    expect(result.history.map((point) => point.at)).toEqual(['a', 'b', 'c'])
    expect(result.history[0].observed).toBe(false)
    expect(result.totals).toEqual({ chat: 3, agent: 3, operator: 1 })
    expect(result.max).toBe(6)
    const stack = result.history[1].segments
    expect(stack.map((segment) => segment.source)).toEqual(['chat', 'agent', 'operator'])
    expect(stack[0].bottom).toBe(0)
    expect(stack[1].bottom).toBeCloseTo(100 / 3)
    expect(stack[2].bottom + stack[2].height).toBeCloseTo(100)
  })

  it('never synthesizes minute counts from old cumulative-only payloads', () => {
    const result = buildOperationTrend({
      history: axis,
      business_operations: { total: 600, rows: [{ source: 'chat', count: 600 }] },
    })
    expect(result.hasActivity).toBe(false)
    expect(result.history.every((point) => !point.observed)).toBe(true)
    expect(result.totals).toEqual({ chat: 0, agent: 0, operator: 0 })
  })

  it('distinguishes observed quiet minutes from unavailable data and rejects malformed counts', () => {
    const result = buildOperationTrend({
      history: axis,
      business_operations: {
        history: [
          { at: 'b', observed: true, sources: { chat: -1, agent: NaN, operator: '3' } },
          { at: 'c', observed: false, sources: { chat: 100 } },
        ],
      },
    })
    expect(result.history.map((point) => point.observed)).toEqual([false, true, false])
    expect(result.hasActivity).toBe(false)
    expect(result.max).toBe(1)
    expect(
      result.history
        .flatMap((point) => point.segments)
        .every((segment) => Number.isFinite(segment.height))
    ).toBe(true)
  })
})
