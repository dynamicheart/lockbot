"""
Platform credential helpers.

Credentials are stored as encrypted JSON in the `credentials` column,
using platform-specific keys:

  Infoflow:  {"webhook_url", "token", "aes_key"}
  Feishu:    {"app_id", "app_secret", "encrypt_key"}
  Slack:     {"bot_token", "signing_secret", "event_url"}
  DingTalk:  {"app_secret"}
"""

import json
import logging

from lockbot.backend.app.bots import encryption

logger = logging.getLogger(__name__)


def encrypt_credentials(creds: dict) -> str:
    """Encrypt a platform credentials dict to a string for DB storage."""
    if not creds:
        return ""
    return encryption.encrypt(json.dumps(creds, ensure_ascii=False))


def decrypt_credentials(bot) -> dict:
    """Decrypt the credentials column and return the platform-specific dict.

    Falls back to old columns (webhook_url/token/aes_key) for unmigrated bots.
    """
    raw = encryption.decrypt(getattr(bot, "credentials", "") or "")

    if raw:
        try:
            return json.loads(raw)
        except Exception:
            logger.warning("Failed to parse credentials JSON for bot %s", getattr(bot, "id", "?"))

    # Fallback: build platform-specific dict from legacy columns
    platform = getattr(bot, "platform", None) or "Infoflow"
    webhook = encryption.decrypt(getattr(bot, "webhook_url", "") or "")
    token = encryption.decrypt(getattr(bot, "token", "") or "")
    aes_key = encryption.decrypt(getattr(bot, "aes_key", "") or "")

    if platform == "Infoflow":
        creds = {}
        if webhook:
            creds["webhook_url"] = webhook
        if token:
            creds["token"] = token
        if aes_key:
            creds["aes_key"] = aes_key
        return creds

    # For other platforms, the old columns may not have been populated.
    # Return empty dict — the adapter will handle missing credentials.
    return {}
