# Active-state migration planning (disabled draft)

This is the active-holder, booking and pending-offer part of migration #88. It is a private comparison planner, not an execution endpoint. It is deliberately not wired into `CutoverCoordinator`; that coordinator still refuses all outstanding allocations. Nothing in this slice mutates state, enrolls users, copies keys, persists mappings or migrates a real bot.

## Reviewed identity input

Every exact Infoflow principal must have a one-to-one reviewed Feishu principal link. The scope includes service instance, migration operation, bot UUID/source revision, internal owner ID, destination application ID/App ID, tenant and chat. The source-account proof and destination signed-group-sender proof are distinct opaque references. The same current reviewing administrator and an unexpired bounded review window are required. Destination proof must establish the exact sender in the expected tenant/application/group; name or contact lookup cannot establish ownership or group membership. The application's own uncached membership is a separate execution prerequisite.

`ReviewedLink` is an internal typed input for a future verified-link repository, not a trustworthy request body or proof verifier. A Python caller can construct a dataclass; this does not confer authority. Before execution is connected, a durable workflow must load references from verified, single-use source and destination challenges, confirm current administrator authority and target membership, and audit the exact reviewed principals. A manually edited mapping JSON cannot become that repository. An explicit administrator override would require its own reason/proof policy; it is not implemented as a fallback here.

Missing, duplicate, many-to-one, differently scoped, unused or expired mappings block planning. Whitelist-only users also need reviewed links. The planner never matches aliases, display names or similarly spelled account names. It does not change the internal resource-bot owner.

## State and policy comparison

The planner deep-copies the allocator state, changing only `user_id` to the linked principal. It preserves node/card identity, access mode, holder order, queue order, holding IDs, acquisition timestamps, requested duration, booked-start timestamps, notification state and origin metadata. A private provenance sidecar retains original and destination principal references alongside resource/holding identity and timing. Public summaries contain counts and deadlines only; they never include those principals or raw task metadata.

A holder's deadline remains exactly `start_time + duration`. A notified booking remains an offer with exactly its original `start_time + 300` claim deadline, matching the current QueueBot policy; it is not converted to a lock and is not re-notified with a fresh timer. Ordinary queued bookings do not expire at `booking_start + requested_duration`: that duration is their requested future allocation, so it is preserved without interpreting it as a current lease.

An expired holder or offer blocks for an explicit lifecycle expiration reconciliation. It is never silently dropped, regranted or extended. A delayed migration must replan against current state once the earliest holding/offer deadline or proof window is reached. This is particularly relevant when an active metrics source requires an authorization-drain wait: wait first or revalidate afterward; do not extend leases to make migration fit.

Resource limits count individual cards for DEVICE/QUEUE_DEVICE and nodes for NODE/QUEUE. The mapping is bijective and preserves each principal's exact resource claims. Existing over-limit state requires administrator review instead of merging principals, raising limits or deleting allocations. `DURATION_WHITELIST` is mapped only using verified links, in its original order. Platform-neutral rules are retained. Unknown rules, source group allowlists or unknown allocation metadata require explicit review; they cannot be copied under an assumption of platform neutrality.

## Execution gates still required

1. Durable verified identity-link repository and signed challenge consumption; current source/target and reviewer checks immediately before use.
2. Durable freeze/drain/checkpoint, original outbox settlement and exact source digest comparison across database/state phases, with process-kill and backup recovery acceptance.
3. Rebuild current declaration/cleanup ownership under destination principal and revision after explicit re-enrollment; old personal/OP/audit authority does not transfer. Historical records remain attributed to the original platform/revision. Unknown embedded task metadata blocks this planner rather than leaking it.
4. Published-application review, uncached own membership and post-creation signed-event receiver proof. No new enterprise-admin API permissions are implied by this slice.
5. Metrics remain optional and separate. Only a configured source with active grants needs its verified expiry/profile/clock barrier; absent metrics must not block migration.
6. Preserve deadlines, queue order, quota and owner checks during real fictional-group acceptance before enabling any active execution API.

`validate_active_state_plan` rejects changed source rules/state, modified target state/rules, different scope/reviewer/owner/revision and elapsed deadlines. A successful comparison still returns `can_commit: false`. It cannot be used as migration authorization.
