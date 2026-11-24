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
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ replace: vi.fn() }),
}))
vi.mock('../../components/ConfigurationCredentialManager.vue', () => ({
  default: { template: '<div>configuration-manager</div>' },
}))
it.each(['user', 'admin', 'super_admin'])('only shows configuration credentials (%s)', async (role) => {
  auth.user.role = role
  const app = createSSRApp(ApiCredentialsView)
  app.config.globalProperties.$t = (key) => key
  const rendered = await renderToString(app)
  expect(rendered).toContain('configuration-manager')
  expect(rendered).not.toContain('auditTab')
})
