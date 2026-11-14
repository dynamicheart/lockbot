import { shallowRef } from 'vue'

// Capture before Vue Router initializes so handoff tickets never remain in its URL.
export const pendingPanelTicket = shallowRef('')
export const pendingCredentialViewTicket = shallowRef('')
const browser = globalThis.window
const [path, query = ''] = (browser?.location?.hash || '').slice(1).split('?')
if (path === '/me') {
  const ticket = new URLSearchParams(query).get('ticket')
  const credentialView = new URLSearchParams(query).get('credential_view')
  if (ticket || credentialView) {
    // Distinct purposes never auto-convert into one another. An ambiguous
    // fragment is discarded rather than selecting one authority implicitly.
    if (credentialView && !ticket) pendingCredentialViewTicket.value = credentialView
    if (ticket && !credentialView) pendingPanelTicket.value = ticket
    browser.history.replaceState(browser.history.state, '', '/#/me')
  }
}
