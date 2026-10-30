# Platform migration preflight (administrator preview)

This is the first read-only delivery for [Infoflow → Feishu migration #88](https://github.com/dynamicheart/lockbot/issues/88). It **cannot migrate a bot** yet. Copying a configuration and creating another independent bot is not a substitute for handing over the same allocation authority.

A current super administrator may call `POST /api/bots/{bot_id}/migration-preflight` using their existing management session. The body contains `application_id` (the existing Lockbot Feishu application record ID) and a destination `chat_id`. Agent keys, OP keys and ordinary administrators cannot call it. Do not place management tokens in URLs or share them with resource Agents.

```json
{
  "application_id": 1,
  "chat_id": "oc_example"
}
```

The response is `Cache-Control: no-store` and always includes `read_only: true`, `can_commit: false`, `reservation_created: false` and `cutover_not_implemented`. This operation does not create a plan, stop a bot, reserve a destination, send messages, validate credentials over the network, write audit events, or mutate resource state. Its configuration digest is an observation identifier, not a commit authorization.

`stored_obligations` counts the admitted live generation under its state lock. It includes expired records awaiting reconciliation; `reservations` includes offered reservations and `offers` is their notified subset. Unavailable/stopped/retiring generations are unknown, not empty. Missing nodes/cards or unrecognized state are blockers. No user identifiers, Agent labels, detailed task notes or credential values are returned. The read lease also fences normal lifecycle replacement while taking the snapshot; the result can become stale immediately afterwards and must be checked again by any future cutover protocol.

The destination checks are local observations only. An existing application/group route blocks takeover. A stored membership event with a different application/tenant blocks trust. Even a matching `present` event does not prove current membership, publication, valid credentials or a functioning receiver; these remain explicitly unverified. The endpoint neither contacts Feishu nor expands enterprise permissions.

Credential work remains necessary before any future cutover:

- Old personal Agent keys must be invalidated at the source revision, with destination re-enrollment.
- OP keys currently check bot ID and stored key, not identity revision. They must be explicitly revoked/reissued; bumping a revision alone is insufficient.
- The bundled metrics gateway accepts a five-minute grant plus 15 seconds of expiry leeway. A future migration must stop **all** issuance paths first and wait at least 315 seconds plus verified issuer/verifier clock offset, or implement and test a separate revocation mechanism. An independently operated or third-party gateway may have different rules and requires separate verification. The response does not assert a countdown has started or that grants have expired.
- Active-state migration still needs verified cross-platform user mapping, absolute deadline/queue preservation, a durable freeze/journal, restart recovery and explicit confirmation. `identity_mapping.unresolved_principals` is unknown (`null`), never a fabricated zero.

Production migration and the administrator cutover UI remain separate deliveries. Do not change database `platform` fields manually or replay lock commands to simulate migration.
