import { describe, it, expect } from 'vitest'
import { createI18n } from 'vue-i18n'
import zh from '../i18n/zh-CN'
import en from '../i18n/en'
import { formatAuditDetail } from './auditDisplay'

const messages = { zh, en }
const keys = [
  'resource_auth.bind',
  'resource_auth.unbind',
  'bot.resource_auth_enabled',
  'bot.resource_auth_disabled',
  'bot.resource_auth_revoke_all',
]

describe('Agent audit localization', () => {
  it('translates all new action codes and switches language without changing stored records', () => {
    const i18n = createI18n({ legacy: false, locale: 'zh', messages })
    const row = { action: 'bot.resource_auth_enabled', operator_role: 'agent' }
    expect(i18n.global.tm('audit.actions')[row.action]).toBe('开启 Agent 访问')
    i18n.global.locale.value = 'en'
    expect(i18n.global.tm('audit.actions')[row.action]).toBe('Enable Agent access')
    for (const locale of [zh, en]) {
      for (const key of keys) expect(locale.audit.actions[key]).toBeTruthy()
      expect(locale.audit.roles.agent).toBe('Agent')
      expect(locale.audit.targetTypes.bot).toBeTruthy()
      expect(locale.botDetail.apiKey).toBe('OP API Key')
      expect(locale.agentActivity.empty).toBeTruthy()
    }
    expect(row.action).toBe('bot.resource_auth_enabled')
  })
  it('labels private task reads in both audit views without changing their action code', () => {
    // BotAudit and AuditLogsView both resolve the persisted action through this map.
    const row = Object.freeze({ action: 'task_private.read' })
    const i18n = createI18n({ legacy: false, locale: 'zh', messages })
    expect(i18n.global.tm('audit.actions')[row.action]).toBe('查看 Agent 详细任务备注')
    i18n.global.locale.value = 'en'
    expect(i18n.global.tm('audit.actions')[row.action]).toBe('Read Agent task notes')
    expect(row.action).toBe('task_private.read')
  })
  it('localizes known metadata while keeping actor, commands and untrusted text intact', () => {
    const result = formatAuditDetail(
      JSON.stringify({
        source: 'api',
        external_user_id: 'alice',
        command: 'lock n1 10m',
        group_notified: false,
        arbitrary: '<script>do not execute</script>',
      }),
      {
        fields: zh.audit.detailFields,
        sources: zh.audit.sources,
        booleans: zh.audit.booleanValues,
        dispatchedNote: zh.audit.dispatchedNote,
      }
    )
    expect(result).toContain('调用来源: Agent API')
    expect(result).toContain('已验证用户: alice')
    expect(result).toContain('命令: lock n1 10m')
    expect(result).toContain('群通知已提交: 否')
    expect(result).toContain('<script>do not execute</script>')
    expect(formatAuditDetail('plain legacy detail', {})).toBe('plain legacy detail')
  })
})

it('renders Agent gate changes as localized configuration values', () => {
  const result = formatAuditDetail(
    { resource_auth_enabled: { from: false, to: true } },
    {
      fields: zh.audit.detailFields,
      booleans: zh.audit.booleanValues,
    }
  )
  expect(result).toBe('允许 Agent 访问: 否 → 是')
})

it('shows retained bot IDs and global private-read targets without rewriting rows', async () => {
  const { auditTarget } = await import('./auditDisplay')
  const oldBot = Object.freeze({ target_type: 'bot', target_id: 25, target_name: null })
  expect(auditTarget(oldBot)).toEqual({ type: 'bot', name: '#25' })
  expect(
    auditTarget({
      action: 'task_private.read',
      target_type: 'bot',
      target_id: null,
      detail: { surface: 'management.global_service_traffic' },
    })
  ).toEqual({ type: 'service', name: 'Lockbot' })
  expect(auditTarget({ target_type: 'bot', target_id: null }).name).toBe('')
  expect(oldBot.target_name).toBe(null)
})
