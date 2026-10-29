import { describe, it, expect } from 'vitest'
import { copyBotTemplate } from './botTemplate'
describe('configuration-only bot template', () => {
  it('retains resource policy while excluding credentials, platform users and live state', () => {
    const source = {
      id: 7,
      uuid: 'old',
      name: 'pool',
      bot_type: 'QUEUE_DEVICE',
      platform: 'Infoflow',
      cluster_configs: [{ node_key: 'n1', devices: ['gpu', 'gpu'] }],
      config_overrides: {
        FORBID_RELOCK: true,
        NODE_ALIASES: { n1: 'node' },
        DISPLAY_DEVICE_MODEL: true,
        DURATION_WHITELIST: ['old-platform-user'],
        _FEISHU_CLIENT: 'secret',
      },
      group_id: 'old-group',
      webhook_url_raw: 'secret',
      aes_key_raw: 'secret',
      token_raw: 'secret',
      api_key_raw: 'secret',
      feishu_application_id: 5,
      current_users: ['alice'],
      booking_list: ['bob'],
      resource_auth_enabled: false,
      resource_api_notify_enabled: true,
    }
    const copy = copyBotTemplate(source)
    expect(Object.keys(copy).sort()).toEqual(
      [
        'name',
        'bot_type',
        'platform',
        'cluster_configs',
        'config_overrides',
        'resource_auth_enabled',
        'resource_api_notify_enabled',
      ].sort()
    )
    expect(copy.config_overrides).toEqual({
      FORBID_RELOCK: true,
      NODE_ALIASES: { n1: 'node' },
      DISPLAY_DEVICE_MODEL: true,
    })
    expect(copy.resource_auth_enabled).toBe(false)
    copy.cluster_configs[0].devices.push('new')
    copy.config_overrides.NODE_ALIASES.n1 = 'changed'
    expect(source.cluster_configs[0].devices).toHaveLength(2)
    expect(source.config_overrides.NODE_ALIASES.n1).toBe('node')
  })
})
