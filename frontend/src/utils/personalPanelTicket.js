import { shallowRef } from 'vue'

// Capture before Vue Router initializes so handoff tickets never remain in its URL.
export const pendingPanelTicket = shallowRef('')
const browser = globalThis.window
const [path, query = ''] = (browser?.location?.hash || '').slice(1).split('?')
if (path === '/me') {
  const ticket = new URLSearchParams(query).get('ticket')
  if (ticket) {
    pendingPanelTicket.value = ticket
    browser.history.replaceState(browser.history.state, '', '/#/me')
  }
}
