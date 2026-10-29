# Verified allocator lifecycle history

This extends the OP request-history example with **allocator state evidence**. HTTP success, request counts, bookings and measured hardware utilization remain different measurements. No resource commands are retried by telemetry and no group messages are sent.

## Commit hook and compatibility

Managed instances attach a backend-owned optional observer to their existing Config object. Core IO calls it after a successful atomic `bot_state.json` replacement, and invalidates it on **any** failed persistence stage. The existing state JSON and database schema are unchanged. The existing non-reentrant state lock remains authoritative: a small context manager also notices an exception after an in-memory mutation but before persistence, before releasing the lock. Successful reads add no observation work. Online administrator state replacement calls the same hook; offline edits are covered by the next generation's baseline boundary.

Each observation performs bounded canonicalization and a synchronous local append only: at most 2048 node/card/user entries, a 512 KiB line, ten 5 MiB files, no network, database event insert, history scan or background queue. An over-limit snapshot or append error does not change an already performed resource operation. It makes `checkpoint.consistent=false` until a later baseline boundary. Records start with a newline so a partial failed append cannot swallow the next recovery baseline. Files are private regular files, refuse links, and use managed paths.

## File schema v1

`DATA_DIR/bots/<id>/resource-lifecycle.jsonl` contains public, allowlisted allocator facts only:

- `schema=lockbot.resource-lifecycle`, `schema_version=1`, bot ID, random generation `session`, monotonically increasing `sequence` and UTC epoch `timestamp`.
- `before_digest`, `after_digest`, `boundary` and the complete canonical after-snapshot. A snapshot contains current allocations and reservations, actor platform/ID, resource, node/device unit, shared/exclusive mode, declared start/deadline, origin and reservation notification state.
- Actual state transitions: acquired, renewed, released, expired, booked, claim-ready, claimed, claim-timeout, booking-removed and mode-changed. A claim links a removed reservation to the new allocation on the same resource/actor. Origins distinguish chat, Agent API, OP, administrative state changes and internal work.

`lease_ref` / `booking_ref` are domain-separated SHA-256 correlations of existing allocator incarnations. Raw `holding_id`, holding proofs, keys, authorization notes, Agent labels, free purposes, OP reasons and arbitrary state fields are never copied. These hashes cannot be submitted as an authorization or holding proof. No personal-key or OP privilege is widened. Task classification history remains in the existing public-category Agent-operation stream; this ledger does not guess categories or attribute hours from free notes.

## Reading and coverage gate

`GET /api/bots/{bot_id}/op/lease-history` requires that bot's OP key before reading and freshly again before returning. A key issued by a super administrator remains ordinary OP authority. The endpoint uses the same private-file checks, 1 MiB scan budget, cursor rotation errors and 1–100 page size as Agent history, with the separate 512 KiB lifecycle line bound. Public schema validation precedes output. Scope is the deployment's current bot ID; importing another bot/instance's files into that directory is unsupported, not an isolation guarantee.

A live instance can attach a read-only tail checkpoint under its state lock. It confirms the session, sequence, canonical digest and observation time only when in-memory state still matches the last successful commit and no failure is pending. It does not write allocator state or extend a lease. A stopped/unavailable instance may have no checkpoint; missing evidence stays unknown.

The standalone stdlib script `scripts/lease_history/report.py` consumes authorized JSONL pages or canonical frames, with explicit `--since` / `--until`. Only consecutive sequence/digest-linked snapshots in a generation, with complete bounded states and monotonic time, form verified intervals. The initial observed snapshot is a baseline: it never backfills older claimed start times. Restart, restored state, a writer failure, missing/rotated records, malformed/conflicting rows, clock reversal or an unmatched tail makes affected windows incomplete. Unknown ranges are excluded from verified segments. A fully covered window receives `verified_allocated_seconds`; otherwise that total is **null**, alongside verified segments and explicit unknown gaps. Starting a new window at a recovered baseline can become verified again.

A lease contributes the intersection of its previous committed snapshot interval and its previous deadline. An early free/OP release ends at the next commit; delayed expiry closes at the known deadline, not sweep time. A renewal observed after the old deadline starts another confirmed segment and **does not fill the expired gap**. Identical event rows do not duplicate time, and repeated requests are not acquisitions unless state actually gains a new incarnation.

NODE/QUEUE produce node subject-grant seconds. DEVICE/QUEUE_DEVICE produce device subject-grant seconds. Shared grants are separated from exclusive grants and count each subject's reservation independently; their sum is not physical card occupancy or utilization. No node-to-card multiplier is guessed. This verifies committed allocation intervals, not workload execution, billing or physical device activity. Administrative/platform credential holders and OS/root remain existing trust boundaries.

Example after an OP Agent has saved each returned page as one JSON line:

```sh
python3 scripts/lease_history/report.py --input authorized-lease-pages.jsonl \
  --since 2026-01-01T00:00:00Z --until 2026-01-02T00:00:00Z
```

Follow every non-null cursor, including empty pages. A 409 requires a new export; do not stitch an old cursor into a complete report. The local input is capped at 20 MiB. The script prints a local JSON report only. Review scope, gaps and public content before any separately authorized summary publication.
