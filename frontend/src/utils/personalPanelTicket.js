import { shallowRef } from 'vue'

// Capture before Vue Router initializes so handoff tickets never remain in its URL.
export const pendingPanelTicket = shallowRef('')
export const pendingCredentialViewTicket = shallowRef('')
export const pendingCredentialRotationTicket = shallowRef('')
const browser = globalThis.window
const [path, query = ''] = (browser?.location?.hash || '').slice(1).split('?')
if (path === '/me') {
  const ticket = new URLSearchParams(query).get('ticket')
  const credentialView = new URLSearchParams(query).get('credential_view')
  const rotation = new URLSearchParams(query).get('credential_rotation')
  if (ticket || credentialView || rotation) {
    // Distinct purposes never auto-convert into one another. An ambiguous
    // fragment is discarded rather than selecting one authority implicitly.
    if ([ticket, credentialView, rotation].filter(Boolean).length === 1) {
      if (credentialView) pendingCredentialViewTicket.value = credentialView
      if (ticket) pendingPanelTicket.value = ticket
      if (rotation) pendingCredentialRotationTicket.value = rotation
    }
    browser.history.replaceState(browser.history.state, '', '/#/me')
  }
}
