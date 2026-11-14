# Infoflow → Feishu resource-bot migration

The design follows the 2026-10-31 reset decision in [#88](https://github.com/dynamicheart/lockbot/issues/88). The current implementation includes a default-off supervised cutover with preview, confirmation, drain, route switch, activation and pre-switch cancellation. See the [operator guide](../usage/platform-migration-preflight.md) for supported deployment configurations and the issue for remaining acceptance gates.

## Product behavior

An administrator opens **Migrate to Feishu** in the existing bot's detail page, selects an approved enterprise application and destination group, reviews blockers, and confirms a cutover. The same resource bot retains its ID/UUID, inventory, rules, utilization configuration and history. Its messaging identity changes. The source group stops accepting resource changes before the destination becomes writable.

One migration has one scheduling authority. This is a channel handover, not two bots with independent state and not concurrent cross-platform access. Existing Infoflow bots outside the selected migration remain unchanged.

The confirmed cutover clears current holders, reservations, offers, source Agent enrollments and current usage declarations. Users enroll again in the selected Feishu group. Inventory, platform-neutral scheduling settings, utilization configuration and historical evidence remain attached to the same bot. Source-platform access rules such as `DURATION_WHITELIST` must be listed in the review and cleared for the destination according to the approved reset.

Deliver read-only review, durable reset and recovery, then administrator confirmation and fictional end-to-end acceptance.

## Identity and data boundaries

| Item | Treatment |
| --- | --- |
| Bot ID/UUID, resource names and allocation authority | Preserve; do not create a second resource pool |
| Inventory, aliases, platform-neutral duration/renewal/quota rules, language, metrics configuration | Preserve and include in plan digest; list identity-bearing exceptions for reset |
| Infoflow webhook/token/AES | Keep an encrypted source-configuration reference for controlled rollback; fence its authority in the typed application registry as well as legacy fields. Never repurpose it as Feishu fields or expose it in preview |
| Feishu application | Reference the existing approved application; validate tenant/app/chat and bot membership; do not copy its secret into migration records |
| Platform user identity | Treat Infoflow ID and Feishu tenant/application-scoped Open ID as different principals; never match by name |
| Personal Agent keys | Invalidate source authority at cutover through identity revision; require destination group re-enrollment. No silent cross-platform transfer |
| OP key | Clear the current key before opening destination admission; reissue explicitly. Current OP authentication compares `Bot.api_key` without a revision check, so a revision bump alone does not revoke it |
| Detailed-note audit grants, read windows, metrics grants and resource-view links | View/audit authority checks revision. Existing metrics JWTs do not carry revision and the independent gateway cannot instantly revoke them: stop issuing source grants, wait out their configured maximum TTL plus verifier leeway and actual clock skew before destination admission (the bundled gateway currently allows 15 seconds of leeway, so its five-minute grants require at least 315 seconds after issuance is stopped), or add and deploy gateway revocation support first. Previously disclosed data cannot be revoked |
| Old logs/history and detailed notes | Preserve original platform, principal and revision; historical access stays within current role/delegation rules. Do not relabel old events as Feishu events |
| Pending notifications, broadcasts, query panels and bind codes | Fence old revision; do not deliver source payloads to the destination group |

Do not put credentials, raw group messages, task notes or personal identity mapping proofs in the public plan or audit summary. Migration journal stores references and digests, not secrets.

## Preflight and review

Preflight is read-only. It returns source identity revision/configuration digest, target application/group references, counts of allocations, bindings and declarations and explicit blockers. A preview is not a reservation or permission to commit later.

Checks include:

- Current super-administrator authority. The ordinary bot update endpoint remains unable to change platform.
- Supported source type/platform, stable bot UUID, healthy recoverable state and no competing lifecycle operation.
- Destination application exists in the expected tenant, server-side credentials validate, proxy/receiver is available, and the application can access the target group.
- Unique `(application_id, chat_id)` and bot route; reject another bot's binding without overwriting or transferring ownership. This restriction is Feishu-specific.
- No pending unknown-result command or incomplete lifecycle journal. Review counts for locks, reservations, offers, current bindings, declarations and source identity rules. Outstanding allocations are part of the reset confirmation.
- Review old/new group, retained settings, keys requiring re-enrollment, outstanding work and expected interruption. Confirmation is tied to an expiring plan digest, not an arbitrary target submitted afterwards.

Application publication and group membership are external facts. They are checked as closely as practical to commit, but provider/network failures remain possible. A destination failure must leave scheduling frozen or recoverably stopped; it must not restore a second writer automatically.

## Cutover protocol and crash recovery

Proposed additive `bot_migrations` records have an unguessable operation ID, bot UUID, source/target revisions, target references, plan digest, phase, state-checkpoint digest and result/error code. An active-operation constraint and the existing lifecycle transition owner protect the same bot. A JSON file plus a DB commit is not an atomic transaction; checkpoint publication and DB phases require explicit recovery.

```mermaid
stateDiagram-v2
    [*] --> planned
    planned --> freezing: commit with matching plan
    freezing --> checkpointed: drain and validate state
    checkpointed --> switched: durable identity and route commit
    switched --> activating: load same resource state
    activating --> completed: destination ready
    freezing --> recovery_required: timeout or uncertain execution
    checkpointed --> recovery_required: checkpoint or commit failure
    switched --> recovery_required: destination cannot start
    activating --> recovery_required: readiness failure
    recovery_required --> completed: reconcile journal and finish once
```

Execution requirements:

1. Acquire the lifecycle transition owner before rereading configuration. Persist admission freeze; all mutation entry points, including Agent/OP, webhook/WS, scheduler and restart/autostart, must honor it. An in-process flag alone does not survive a crash.
2. Retire source generation, reject new writes and drain admitted operations. A drain timeout is an uncertain result, not permission to replay commands or start the target. Keep the checkpoint compatible with scheduler/expiration behavior.
3. Reread state and compare the plan. Bind the expiring confirmation to the exact destination, source identity, reviewed counts and snapshot digest. Abort on change and obtain a new confirmation.
4. Drain and durably settle the source lifecycle outbox before changing revision: recovery currently rejects envelopes from another revision. Preserve old frames unchanged and create a destination-revision baseline/coverage boundary; never relabel old frames or discard pending evidence. Write and fsync the checkpoint and operation evidence before switching. Transactionally update the platform/application/route, bump revision, fence old credentials/notifications and record switched phase. Enforce unique group binding in the DB as well as preflight.
5. Publish a clean runtime snapshot at the destination revision before starting the Feishu generation. Use a durable migration phase and an idempotent checkpoint so restart after either the file write or DB commit does not repeat the reset or restore a source writer. Keep it frozen until readiness and state digest checks pass. Record completion before opening mutation admission.
6. Retrying the operation ID returns/reconciles its existing result. A different target or digest conflicts. Startup discovers incomplete operations and keeps them unavailable until reconciled; it must not autostart the obsolete channel.

A verified externally changed target route or source snapshot blocks recovery for operator review. Never infer success from a prior HTTP timeout or `status=stopped` alone.

## Reset and re-enrollment

Preserve resource IDs and neutral inventory metadata. Clear runtime holders, queues and offers from the destination snapshot. Retain old revisions and their immutable audit records. Revoke current bindings and clear old OP and view credentials, pending deliveries, usage declarations and source-platform identity rules through their authoritative stores. Independently verify the configured metrics verifier's residual grant lifetime before activation.

The destination begins with no inherited platform principals. A successful destination onboarding must prove the new identity in the selected Feishu group.

## Rollback

Before identity switch, recover the source generation only after proving the target never became writable. After switch, use a controlled reverse migration with the same freeze/drain protocol and current state; never restore an old DB/state snapshot over newer allocations. Credentials and links do not become valid again by decreasing the identity revision. Revisions increase monotonically, and source credentials need explicit replacement/re-enrollment on a reverse move.

Backups are disaster-recovery evidence, not a cross-platform undo button. Preserve database/state/encryption keys together. A code rollback to an older version unaware of migration phases is unsupported while a migration is in progress.

## Acceptance gates

- Existing Infoflow message formatting, resource commands and same-group multiple-bot behavior remain unchanged outside a migration.
- Permission and route takeover denial; stale plan, revoked application, cross-tenant and concurrent cutover conflicts.
- Freeze all write paths; source delivery delayed until after cutover cannot mutate the target.
- Fault injection before/after checkpoint publication, identity transaction and target activation; process restart does not create a second writer or replay allocations.
- Old Agent/OP/view/audit/metrics access is denied to the extent promised, with a documented short-lived-grant policy; public schemas do not disclose detailed audit authority.
- Fictional integration with a nonempty source: verify cleared holders, reservations, offers and live declarations, deny old credentials, re-enroll in the selected destination, and compare preserved inventory and historical attribution.
- UI exposes phase/blockers/recovery without secrets, resets old request/download state on bot switches, and does not claim a timeout rolled back.
- Current-head CI and independent review precede squash merge; a tagged artifact, formal deployment and corporate-app approval are separately verified.
