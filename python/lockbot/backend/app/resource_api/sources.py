"""Transport discriminators for the bot command log.

A standalone module with no imports, so both the legacy bot router and the agent API can
use the same values without an import cycle between them.
"""

#: A human typing in the IM group.
SOURCE_CHAT = "chat"
#: An agent calling the resource API with a personal credential.
SOURCE_API = "api"
#: The legacy per-bot service key (operator eviction).
SOURCE_SERVICE_KEY = "service_key"

ALL_SOURCES = (SOURCE_CHAT, SOURCE_API, SOURCE_SERVICE_KEY)
