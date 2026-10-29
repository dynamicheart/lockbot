// Copy configuration by allowlist; identities, credentials and live state never cross.
const RULES = [
  'DEFAULT_DURATION',
  'MAX_LOCK_DURATION',
  'TIME_ALERT',
  'FORBID_RELOCK',
  'MAX_RESOURCES_PER_USER',
  'EARLY_NOTIFY',
  'LANGUAGE',
  'NODE_ALIASES',
  'SHOW_NODE_ALIAS',
  'ALLOW_USER_ALIAS',
  'DISPLAY_DEVICE_MODEL',
]
const object = (value, fallback) =>
  typeof value === 'string' ? JSON.parse(value) : value || fallback
export function copyBotTemplate(source) {
  const overrides = object(source.config_overrides, {})
  return JSON.parse(
    JSON.stringify({
      name: source.name,
      bot_type: source.bot_type,
      platform: source.platform,
      cluster_configs: object(source.cluster_configs, {}),
      config_overrides: Object.fromEntries(
        RULES.filter((key) => overrides[key] !== undefined).map((key) => [key, overrides[key]])
      ),
      resource_auth_enabled: source.resource_auth_enabled !== false,
      resource_api_notify_enabled: source.resource_api_notify_enabled !== false,
    })
  )
}
