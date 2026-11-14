# Platform migration preflight (administrator preview)

This is the first read-only delivery for [Infoflow → Feishu migration #88](https://github.com/dynamicheart/lockbot/issues/88). It **cannot migrate a bot** yet. Copying a configuration and creating another independent bot is not a substitute for handing over the same allocation authority.

The administrator preview is also available in an Infoflow bot's detail page under **Configuration → Platform migration → Migrate to Feishu: preflight**. Select a configured application and enter the destination Chat ID, then check the conditions. This panel displays the response counts, blockers and explicitly unverified conditions; it has no cutover, resource release or credential-rotation control. Closing the panel, changing the target or navigating bots discards the observation. Ordinary administrators and users do not see this entry.

A current super administrator may call `POST /api/bots/{bot_id}/migration-preflight` using their existing management session. The body contains `application_id` (the existing Lockbot Feishu application record ID) and a destination `chat_id`. Agent keys, OP keys and ordinary administrators cannot call it. Do not place management tokens in URLs or share them with resource Agents.

```json
{
  "application_id": 1,
  "chat_id": "oc_example"
}
```

The response is `Cache-Control: no-store` and always includes `read_only: true`, `can_commit: false`, `reservation_created: false` and `cutover_not_implemented`. This operation does not create a plan, stop a bot, reserve a destination, send messages, validate credentials over the network, write audit events, or mutate resource state. The configuration digest identifies observed settings. When the local review conditions pass, the server also returns an opaque review digest covering the current source state, affected authorizations and target identity. It changes if a lease is renewed while counts remain the same. Neither digest authorizes a commit.

`stored_obligations` counts the admitted live generation under its state lock. It includes expired resource records awaiting reconciliation. `reservations` includes offered reservations and `offers` is their notified subset. Agent binding counts include unexpired, unrevoked bindings for the current bot identity. Usage counts include unexpired declarations attached to those bindings. Unavailable, stopped or retiring generations leave the counts unknown. Missing inventory or unrecognized state blocks further work. The preview reports the number of effective `DURATION_WHITELIST` entries because that rule uses source-platform user IDs. It returns no principals, Agent labels, task notes or credential values. Any future cutover must recheck the observation after draining the source writer.

A complete local observation also issues a review receipt valid for five minutes. The same signed-in super administrator may send its token and the original target to `POST /api/bots/{bot_id}/migration-review` to check that the scope still matches. A changed or expired receipt returns HTTP 409. The check is read-only and still returns `can_commit: false`. Keep the receipt in the current review flow.

The destination checks are local observations only. An existing application/group route blocks takeover. A stored membership event with a different application/tenant blocks trust. Even a matching `present` event does not prove current membership, publication, valid credentials or a functioning receiver; these remain explicitly unverified. The endpoint neither contacts Feishu nor expands enterprise permissions.

Credential work remains necessary before any future cutover:

- Old personal Agent keys must be invalidated at the source revision, with destination re-enrollment.
- OP keys currently check bot ID and stored key, not identity revision. They must be explicitly revoked/reissued; bumping a revision alone is insufficient.
- The bundled metrics gateway accepts a five-minute grant plus 15 seconds of expiry leeway. A future migration must stop **all** issuance paths first and wait at least 315 seconds plus verified issuer/verifier clock offset, or implement and test a separate revocation mechanism. An independently operated or third-party gateway may have different rules and requires separate verification. The response does not assert a countdown has started or that grants have expired.
- The planned cutover clears live source obligations and authorizations and requires destination re-enrollment. It retains resource settings and historical records. The response reports this decision as `reset_policy: "clear_live_state_and_reenroll"`. A durable freeze, restart recovery and explicit confirmation are still required.

## Test deployment cutover API

A deployment operator can enable the supervised API with `LOCKBOT_ENABLE_PLATFORM_MIGRATION=1`. The default leaves preflight and review available and disables begin and continue requests.

A signed-in super administrator sends the reviewed target, `review_receipt` and `confirm_clear_live_state: true` to `POST /api/bots/{bot_id}/migration`. Before freezing, the server checks the supported Feishu WebSocket receiver, the application bot identity and access to the selected group. Deployments whose source uses short-lived metrics require a verified expiry policy. The API refuses those migrations before freezing until that policy is supported.

Use `GET /api/bots/{bot_id}/migration` to recover the operation ID or inspect its phase. Send a POST to `/api/bots/{bot_id}/migration/{operation_id}/continue` to advance the same operation. Inspect status after a timeout. If the process was interrupted before recording the drain, use Continue to reconcile the canonical source state file. The server settles matching source-revision lifecycle evidence and repeats the original scope comparison before proceeding. It keeps the operation fenced if the file is unreadable or the reviewed source changed. The administrator UI and test-tenant delivery acceptance remain separate.
