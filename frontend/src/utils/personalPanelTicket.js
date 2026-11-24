import { shallowRef } from 'vue'
import { personalPanelWebDisabled } from './personalPanelWeb'

// Capture before Vue Router initializes so handoff tickets never remain in its URL.
export const pendingPanelTicket = shallowRef('')
export const pendingCredentialViewTicket = shallowRef('')
export const pendingCredentialRotationTicket = shallowRef('')
export function capturePersonalPanelTickets(tickets) {
  const proofs = [
    ['ticket', pendingPanelTicket],
    ['credential_view', pendingCredentialViewTicket],
    ['credential_rotation', pendingCredentialRotationTicket],
  ].filter(([name]) => Object.hasOwn(tickets, name))
  if (!personalPanelWebDisabled && proofs.length === 1) {
    const [name, target] = proofs[0]
    const value = tickets[name]
    if (typeof value === 'string' && value) target.value = value
  }
  return proofs.length > 0
}

const browser = globalThis.window
const [path, query = ''] = (browser?.location?.hash || '').slice(1).split('?')
if (path === '/me') {
  const params = new URLSearchParams(query)
  if (
    capturePersonalPanelTickets(
      Object.fromEntries(
        ['ticket', 'credential_view', 'credential_rotation']
          .map((name) => [name, params.getAll(name)])
          .filter(([, values]) => values.length)
          .map(([name, values]) => [name, values.length === 1 ? values[0] : values])
      )
    )
  ) {
    browser.history.replaceState(browser.history.state, '', '/#/me')
  }
}
