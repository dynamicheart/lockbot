/* eslint-disable vue/one-component-per-file -- Component stubs render tab labels. */
import { expect, it, vi } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import ApiCredentialsView from './ApiCredentialsView.vue'

const auth = vi.hoisted(() => ({
  user: { id: 1, role: 'user' },
  token: 'test-session',
  get isSuperAdmin() {
    return this.user.role === 'super_admin'
  },
}))

vi.mock('../../stores/auth', () => ({ useAuthStore: () => auth }))
vi.mock('../../components/ConfigurationCredentialManager.vue', () => ({
  default: { template: '<div>configuration-manager</div>' },
}))
vi.mock('../../components/AuditCredentialManager.vue', () => ({
  default: { template: '<div>audit-manager</div>' },
}))

it.each([
  ['user', false],
  ['admin', false],
  ['super_admin', true],
])('shows the audit credentials tab only to super administrators (%s)', async (role, showAudit) => {
  auth.user.role = role
  const app = createSSRApp(ApiCredentialsView)
  app.config.globalProperties.$t = (key) => key
  app.component('ElTabs', { template: '<div><slot /></div>' })
  app.component('ElTabPane', { props: { label: { type: String, required: true } }, template: '<span>{{ label }}</span>' })
  const rendered = await renderToString(app)

  expect(rendered).toContain('configuration-manager')
  expect(rendered.includes('configurationCredentials.auditTab')).toBe(showAudit)
  expect(rendered).not.toContain('audit-manager')
})
