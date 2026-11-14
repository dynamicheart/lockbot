# Migrate an Infoflow resource bot to Feishu

The migration keeps the same resource bot, inventory, scheduling configuration and history. It clears live locks, reservations, offers and source authorizations. Users enroll again in the destination group. The cutover is disabled by default. [Track rollout and acceptance](https://github.com/dynamicheart/lockbot/issues/88).

## Preview the reset

A super administrator can open **Migrate to Feishu** in the Infoflow bot's configuration, select a configured Feishu application and enter the destination Chat ID. The preview shows affected counts, blockers and conditions that still require verification. Closing the dialog, changing the target or navigating to another bot discards the preview.

A current super administrator may call `POST /api/bots/{bot_id}/migration-preflight` using their existing management session. The body contains `application_id` (the existing Lockbot Feishu application record ID) and a destination `chat_id`. Agent keys, OP keys and ordinary administrators cannot call it. Do not place management tokens in URLs or share them with resource Agents.

```json
{
  "application_id": 1,
  "chat_id": "oc_example"
}
```

The response is `Cache-Control: no-store` and includes `read_only: true`, `can_commit: false` and `reservation_created: false`. The preview only reads local state. On an enabled deployment with a valid review receipt, the response also sets `begin_available: true` and omits the `cutover_not_implemented` blocker. A separate confirmation starts the migration. The configuration digest identifies observed settings. When the local review conditions pass, the server also returns an opaque review digest covering the current source state, affected authorizations and target identity. It changes if a lease is renewed while counts remain the same.

`stored_obligations` counts the admitted live generation under its state lock. It includes expired resource records awaiting reconciliation. `reservations` includes offered reservations and `offers` is their notified subset. Agent binding counts include unexpired, unrevoked bindings for the current bot identity. Usage counts include unexpired declarations attached to those bindings. Unavailable, stopped or retiring generations leave the counts unknown. Missing inventory or unrecognized state blocks further work. The preview reports the number of effective `DURATION_WHITELIST` entries because that rule uses source-platform user IDs. It returns no principals, Agent labels, task notes or credential values. Any future cutover must recheck the observation after draining the source writer.

A complete local observation also issues a review receipt valid for five minutes. The same signed-in super administrator may send its token and the original target to `POST /api/bots/{bot_id}/migration-review` to check that the scope still matches. A changed or expired receipt returns HTTP 409. The check is read-only and still returns `can_commit: false`. Keep the receipt in the current review flow.

The destination checks are local observations only. An existing application/group route blocks takeover. A stored membership event with a different application/tenant blocks trust. Even a matching `present` event does not prove current membership, publication, valid credentials or a functioning receiver; these remain explicitly unverified. The endpoint neither contacts Feishu nor expands enterprise permissions.

The reset has the following credential effects:

- Source Agent bindings are revoked. Users enroll again in the destination group.
- The source OP key is cleared. Reissue it after cutover if needed.
- Short-lived metrics grants expire before the route switches. For a deployment using signed grants, the operator must first verify the expiry policy for every gateway that accepted grants from this Lockbot instance, including candidate sources used in connection tests. Independent credentials remain under their datasource's own authority.

### Verify the metrics gateway policy

Verify that each gateway enforces the Lockbot grant maximum of 300 seconds and at most 15 seconds of expiry leeway. Establish an upper bound in whole seconds on how far each verifier clock can lag the issuer throughout the migration window. Include every replica behind an audience and every audience that may still accept an outstanding grant. An unverified or unreachable verifier must be excluded from migration eligibility until its outstanding grants have verifiably expired or its trust has been revoked.

Set `LOCKBOT_MIGRATION_METRICS_CLOCK_SKEW` in the service environment to a JSON map of canonical metrics audiences to the verified clock lag bounds, for example `{"https://metrics.example.invalid":10}`. Use the exact audience with any path prefix and no trailing slash. While this policy is set, Lockbot only issues grants for those audiences. Keep the policy active through the operation. The setting attests to the verifier configuration and clocks, which Lockbot does not probe.

For signed grants, the route switch waits at least 315 seconds plus the largest configured lag after the source drains. Lockbot records the minimum wait with the operation. Changing the policy cannot shorten that wait. The migration status gives the time to refresh before continuing, and the existing metrics configuration is retained.

## Run a supervised cutover

The deployment operator enables cutover with `LOCKBOT_ENABLE_PLATFORM_MIGRATION=1`. Before starting, connect the Feishu WebSocket receiver for the approved application, add its bot to the target group, and make sure Lockbot has recorded its membership.

1. Open the preview and check the destination and affected counts.
2. Confirm that existing allocations and reservations will be cleared and users will enroll again. Start the migration.
3. Use **Continue** to advance the recorded operation through drain, route switch and activation. Reopen the dialog to recover its status after a timeout.
4. Use **Cancel** before the route switches to resume the source from its saved state. After the switch, use **Continue** to finish activation.

Test-tenant delivery and destination re-enrollment must be verified before formal rollout.

## Test deployment cutover API

A signed-in super administrator sends the reviewed target, `review_receipt` and `confirm_clear_live_state: true` to `POST /api/bots/{bot_id}/migration`. Before freezing, the server checks the supported Feishu WebSocket receiver, the application bot identity and access to the selected group. A source with an unverified metrics policy is blocked in preflight and cannot be frozen. During the expiry wait, status returns `metrics_grants_wait_until` in UTC and Continue is unavailable.

Use `GET /api/bots/{bot_id}/migration` to recover the operation ID or inspect its phase. Send a POST to `/api/bots/{bot_id}/migration/{operation_id}/continue` to advance the same operation. Inspect status after a timeout. If the process was interrupted before recording the drain, use Continue to reconcile the canonical source state file. The server settles matching source-revision lifecycle evidence and repeats the original scope comparison before proceeding. It keeps the operation fenced if the file is unreadable or the reviewed source changed. Before the route switches, use Cancel in the migration dialog or POST to the same operation with the `/cancel` suffix. Cancellation resumes the original bot from its verified on-disk state without clearing its current resources or changing its identity revision. After the route switches, finish the operation through Continue. Test-tenant delivery acceptance remains separate.
