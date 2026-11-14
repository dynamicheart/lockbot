# Resource webpage link lifetime

The platform administrator sets `resource_view_link_days` in site settings. A bot
owner may set `RESOURCE_VIEW_LINK_DAYS` in that bot's configuration overrides. A
missing bot override inherits the platform setting; it differs from `0`, which
explicitly selects no automatic expiry. The platform default remains one day.
Values are `0` (permanent) or an integer from 1 through 30 days.

Changing the effective lifetime applies on the next authorized link retrieval
(group query, personal Agent context, or bot-detail page). That retrieval replaces
the existing code, making the old link invalid. Ordinary webpage polling never
creates, renews or changes a link. Finite links renew lazily after expiry.

An owner or super administrator can explicitly regenerate the code with
`POST /api/bots/{bot_id}/view-link/regenerate`. It immediately invalidates the
previous compact and legacy codes. Personal Agent keys, OP keys, bot identities
and saved resource state are unaffected. A read-only code or personal Agent key
cannot call this management endpoint. Regeneration is refused when resource
access is disabled. Disabling Agent access does not revoke an already shared
read-only link. Regenerate it before disabling issuance when the old code must be
invalidated; disabling or stopping a bot is not a substitute for code rotation.

A permanent link is a bearer read capability: anyone who receives it can keep
reading the permitted bot's public resource state until it is regenerated or the
bot identity/deletion invalidates it. It does not grant personal/OP operations or
detailed task notes. Clients receive `view_expires_at: null`; the existing database
and signature formats retain an internal far-future marker without a schema
migration. Existing finite compact and legacy links retain their formats.

This is separate from the data-source `metrics:read` authorization. That grant
keeps its short lifetime even when the resource webpage link is permanent. A long
webpage lifetime cannot guarantee browser reachability, CORS or data freshness.
