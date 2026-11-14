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

it('strips a separate credential-view proof without turning it into a login proof', async () => {
  vi.resetModules()
  const replaceState = vi.fn()
  vi.stubGlobal('window', {
    location: { hash: '#/me?credential_view=synthetic-view-proof' },
    history: { state: null, replaceState },
  })
  const { pendingPanelTicket, pendingCredentialViewTicket } = await import('./personalPanelTicket')
  expect(pendingPanelTicket.value).toBe('')
  expect(pendingCredentialViewTicket.value).toBe('synthetic-view-proof')
  expect(replaceState).toHaveBeenCalledWith(null, '', '/#/me')
})

it('discards ambiguous login and credential-view purposes while clearing the fragment', async () => {
  vi.resetModules()
  const replaceState = vi.fn()
  vi.stubGlobal('window', {
    location: { hash: '#/me?ticket=login&credential_view=view' },
    history: { replaceState },
  })
  const { pendingPanelTicket, pendingCredentialViewTicket } = await import('./personalPanelTicket')
  expect(pendingPanelTicket.value).toBe('')
  expect(pendingCredentialViewTicket.value).toBe('')
  expect(replaceState).toHaveBeenCalledOnce()
})

it('strips a dedicated rotation fragment and rejects any mixed ticket purposes', async () => {
  for (const hash of [
    '#/me?credential_rotation=rotate',
    '#/me?credential_rotation=rotate&credential_view=view',
  ]) {
    vi.resetModules()
    const replaceState = vi.fn()
    vi.stubGlobal('window', { location: { hash }, history: { replaceState } })
    const { pendingPanelTicket, pendingCredentialViewTicket, pendingCredentialRotationTicket } =
      await import('./personalPanelTicket')
    expect(pendingPanelTicket.value).toBe('')
    expect(pendingCredentialViewTicket.value).toBe('')
    expect(pendingCredentialRotationTicket.value).toBe(hash.includes('&') ? '' : 'rotate')
    expect(replaceState).toHaveBeenCalledOnce()
  }
})
