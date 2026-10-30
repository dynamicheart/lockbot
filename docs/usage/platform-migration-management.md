# Reset live state and migrate

The current user decision is: “Clear all live runtime state during platform migration. Preserve the resource configuration, and have users enroll again on the destination platform.” This English translation is recorded in [issue #88](https://github.com/dynamicheart/lockbot/issues/88).

A current super-administrator reviews the exact target application/group and this bot's authoritative live-state counts. The preview includes holders, reservations, offers, Agent authorizations, current declarations, OP keys and source-user allowlist entries. Both target review and destructive-reset acknowledgement are required to create a plan, bound to the preview digest. Before execution, a second confirmation names the target and lists all approved counts. Unknown counts are not treated as zero.

The operation displays its exact phase, target digest, reset-applied state and approved counts. Advance and abort are explicit; there are no background writes, automatic retries or command replay. An uncertain outcome requires an explicit status read. If source state changed, the stale plan must be aborted and fresh counts reviewed; the client never forces the changed reset. A cleared runtime state is not restored by aborting the remaining operation.

Resource inventory/configuration and immutable historical audit records are preserved. Destination users enroll again; no active allocations or queues are carried across and no identity pairing is required. Single-writer fencing, identity/grant invalidation and durable recovery remain server responsibilities. Software upgrades themselves do not clear unrelated bots' state.

The interface remains unavailable when the management capability is absent or execution is disabled. Source implementation/tests are not evidence of a real migration, provider acceptance or formal rollout. Follow the [release checklist](../../RELEASE_CHECKLIST.md).

Old migration drafts #100/#115/#116/#118 are not the reset release path. Future transport/allocator separation (#120) and official CLI/SDK onboarding (#121) are deferred, non-blocking issues; they add no current implementation requirement.
