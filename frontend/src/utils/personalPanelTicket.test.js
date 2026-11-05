import { afterEach, expect, it, vi } from 'vitest'

afterEach(() => vi.unstubAllGlobals())

it('removes a personal handoff before router initialization and retains it only in memory', async () => {
  vi.resetModules()
  const replaceState = vi.fn()
  vi.stubGlobal('window', {
    location: { hash: '#/me?ticket=synthetic-ticket' },
    history: { state: null, replaceState },
  })
  const { pendingPanelTicket } = await import('./personalPanelTicket')
  expect(pendingPanelTicket.value).toBe('synthetic-ticket')
  expect(replaceState).toHaveBeenCalledWith(null, '', '/#/me')
})

it('leaves other routes and their parameters untouched', async () => {
  vi.resetModules()
  const replaceState = vi.fn()
  vi.stubGlobal('window', {
    location: { hash: '#/bots?ticket=unrelated' },
    history: { replaceState },
  })
  const { pendingPanelTicket } = await import('./personalPanelTicket')
  expect(pendingPanelTicket.value).toBe('')
  expect(replaceState).not.toHaveBeenCalled()
})
