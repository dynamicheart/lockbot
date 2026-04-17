"""Platform registry for LockBot.

To add a new IM platform:
1. Create python/lockbot/core/platforms/<platform>.py implementing MessageAdapter
2. Import it here and add it to PLATFORM_REGISTRY
3. Super admin enables it in Site Settings → it appears in BotForm
"""

from lockbot.core.platforms.infoflow import InfoflowAdapter
from lockbot.core.platforms.slack import SlackAdapter

# Maps the platform name (stored in Bot.platform DB field) to its adapter class.
# Only platforms listed here can be enabled by admins.
PLATFORM_REGISTRY: dict[str, type] = {
    "Infoflow": InfoflowAdapter,
    "Slack": SlackAdapter,
}
