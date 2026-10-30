# Migration target readiness observations

This is a read-only diagnostic for migration #88. It does not enable the draft cutover coordinator. It complements allocation/configuration preflight; it does not replace it.

A current super administrator can call `POST /api/bots/{bot_id}/migration-target-readiness` with `application_id` and `chat_id`, the same body as migration preflight. The response is non-cacheable. The endpoint has a six-per-minute request limit; provider HTTP calls use fixed timeouts, current credentials and the configured Feishu proxy. There are no provider sends, new routes, journal operations, source lifecycle changes or per-view audit writes.

The probe compares the application's own bot Open ID through the provider API and then queries only whether that same bot is in the selected group. It does not list members, enumerate chats or query contacts. The membership request is uncached and strictly requires a Boolean response. Invalid IDs, provider failures and permission errors cannot become positive proof. Raw upstream bodies, tokens and proxy addresses are not returned. If the source identity, application identity/credentials or administrator role change while the request runs, observations are invalidated or refused.

The provider's [own-membership API](https://open.feishu.cn/document/server-docs/group/chat-member/is_in_chat) determines membership from the token identity. Permission availability must be checked against the current official documentation and enterprise configuration; a failed probe does not automatically request additional permissions. The official SDK models the result as `data.is_in_chat: bool`.

`receiver_observation` is explicitly local lifecycle data. Even a recent process handshake does not prove that the correct group event reached this server or that the application is published for all intended members. Cached group names and old membership events are not substituted for the provider probe.

Every response therefore retains `can_commit: false` and explicit blockers for publication/usable range, end-to-end receiver proof, metrics authority/clock verification when a data source is configured, and disabled migration execution. Bots without an optional data source do not acquire a metrics blocker. A publication review or signed-event challenge protocol, deployment clock evidence, independent fault/backup acceptance and authenticated execution routes are separate work. A successful probe must never be passed as the cutover coordinator's production validator by simply returning `True`.

Active-state migration still requires verified source/destination principal links, exact lock deadlines and queue order/provenance preservation. No diagnostic or empty-state cutover completes that requirement.
