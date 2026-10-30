# Durable empty-state platform cutover (draft implementation)

This is implementation work for [#88](https://github.com/dynamicheart/lockbot/issues/88), not production migration support yet. The coordinator has no HTTP commit endpoint and no default production target validator. Do not deploy it as an available migration feature or call its private methods against a real bot. Active-lock/queue identity migration remains a separate required delivery.

## Implemented boundary

`CutoverCoordinator` operates on the same bot ID/UUID and state directory. Planning records a ten-minute source/configuration and destination-application digest, current super-administrator actor and an opaque operation ID. Reusing the ID returns the existing operation; another target conflicts. A partial unique index allows only one nonterminal operation per bot. Creating a plan does not freeze the bot or reserve a Feishu group.

Confirmation checks current source identity/configuration, current super-administrator role, empty stored allocations/reservations/offers, effective identity-based duration rules, the live target-validator dependency and a verified clock-offset bound. It commits `freezing` before draining the old generation. The durable journal is loaded into the manager admission registry before ordinary startup; unknown/incomplete phases and unsupported journal versions remain frozen. Normal lifecycle transitions and write admissions, including reserved scheduler admissions, cannot bypass this fence. Only a matching operation ID on the coordinator's thread permits lifecycle work; it does not permit resource commands.

The saga progresses through:

```text
planned → freezing → checkpointed → switched → activating → completed
                         │
                         └─ wait until metrics_not_before; retry same operation, no sleeping request
planned/freezing/checkpointed → aborting → aborted
```

The source generation drains before checkpoint publication. Stored obligations are rechecked after draining; a late already-admitted lock is retained and blocks migration. The source lifecycle outbox is settled and its persisted frame IDs must be found in the durable derivative journal. The private checkpoint and its directory are fsynced before their hashes enter the database. A crash after checkpoint publication can reconcile the same resource state without replacing conflicting evidence.

When a source data source can issue grants, the metrics barrier stops new signing and waits at least 315 seconds plus the deployment's verified maximum issuer/verifier clock offset. The bundled gateway has a five-minute grant and 15-second expiry leeway. Independent credentials and unverified gateway profiles are blockers; a separate metrics-validator dependency must establish the relevant authority contract. Waiting is a durable phase returned to the caller, not a blocked HTTP request. A bot without an optional data source has no metrics authorization or clock barrier.

The reviewed post-switch configuration digest is rechecked before target activation or recovery. The identity transaction changes the platform and group, adds the unique Feishu route, increments the revision, sets a new enrollment boundary, revokes personal bindings and clears the OP key. Old source transport ciphertext remains an inactive rollback configuration; it is not moved into Feishu credential fields. Target activation uses the same empty resource state and writes a new-revision atomic lifecycle baseline while mutation admission remains frozen. Only a committed completion opens the target. No resource commands are replayed.

Source Agent, OP, view and detailed-note scope cannot silently cross the revision. Metrics/view signing checks both the freeze and a fresh identity tuple, so work retaining an old ORM object cannot issue grants or downgrade the new view window after the fence is released. Ordinary Agent confirmation and detailed-note credential issuance also refuse the frozen bot. Existing source read responses or already disclosed data cannot be recalled.

Before `switched`, an explicit abort drains the source again and restarts its **current** persisted source state. It does not restore the old checkpoint over late allocations, renew deadlines, change source revision or rotate keys. After `switched`, abort is refused: moving back requires a separately confirmed reverse migration. Startup never chooses the old channel automatically. Failed and unknown steps retain the freeze and a safe error code.

## Storage and upgrade compatibility

- Additive `bot_migrations` table and partial active-operation index only; no existing column is renamed, removed or repurposed.
- Existing bots, personal bindings, transport secrets and OP keys are untouched when no migration is confirmed. Normal startup restores an empty fence registry when no incomplete records exist.
- The journal stores IDs, revisions, digests, phases and timestamps, not raw credentials, messages, detailed notes or user-link proofs. Application/actor IDs in terminal history are logical references so later application/user removal does not erase migration evidence; target identity is rechecked against the stored digest before execution.
- Private checkpoint files live inside the managed bot directory. Never import a selected bot from an archive containing an incomplete migration as a fresh, unfrozen bot. Existing-target restore is also fenced by the manager transition owner. Whole-deployment disaster recovery still requires the matching DB, state, checkpoint evidence and encryption keys; the current ordinary backup archive is not a guaranteed consistent in-progress migration snapshot.
- Do not roll code back to a version that ignores incomplete migration records. Clear or recover an operation using migration-aware code first. Database readability alone does not prove safe rollback.

## Tests and remaining release gates

Tests use a real `BotManager`, private temporary state files and SQLite with a fake Feishu readiness validator. They exercise seven injected-exception boundaries followed by fresh-manager journal/fence reconstruction (not OS-level process-kill tests), nonblocking grant waiting, late admitted writes, stale ORM signing, credential revision fencing, management mutation denial, route takeover denial, actor-role revocation and source abort without deadline changes. They do not establish provider publication, proxy reachability, real group membership or actual corporate message delivery.

Before this draft can become enabled product behavior, complete and independently review:

1. A production target validator that proves the exact tenant/application, current bot membership, approved/public application configuration and receiver readiness through supported provider APIs. Cached names or a local “process running” badge are insufficient.
2. A deployment-owned metrics authority/clock verifier; third-party gateway policy must not be assumed to match the bundled gateway.
3. Authenticated management plan/confirm/status/abort endpoints with explicit confirmation tied to the reviewed operation/digests, role/token rechecks, safe audit records, bounded request budgets and UI phase/recovery behavior.
4. Additional failure/concurrency checks around SQL uncertain commit outcomes, fsync/rename failures, invalid outbox recovery, post-switch target application changes, backup recovery and abandoned plans. Do not interpret an exception as proof that a write did not commit.
5. Real fictional-group migration acceptance, unchanged Infoflow formatting/regression and final-head CI. No real or formal bot has been migrated by this implementation.
6. Verified cross-platform identity linking and active-state migration preserving absolute deadlines, queue order, proof ownership and historical attribution. Empty-state success does not complete this requirement.
