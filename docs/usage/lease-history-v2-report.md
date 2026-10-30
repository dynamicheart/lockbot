# V2 lifecycle export and grant-time report

`scripts/lease_history/report_v2.py` is a separate Python-standard-library consumer of the public OP lifecycle contract. Keep its sibling `report.py` beside it: the new consumer reuses only pure state/time/digest validation, never the schema-1 chain analyzer. The existing schema-1 endpoint and report remain unchanged.

This is a local, explicitly authorized OP read. It does not issue credentials, run resource commands, send group reports or query hardware metrics. An OP key does not grant access to detailed Agent purpose notes. Check that the installed service advertises `allocator_lifecycle_audit_v2`; unsupported endpoints and expired/revoked credentials stop collection without a retry.

## Collect from one trusted origin

Provide a bot-specific OP key in an owned, single-link regular file with mode `0600`. Do not paste it into command arguments, a URL, a public configuration or a shell history. Acquire it through the normal authorized administrator flow, and revoke a temporary key afterward. The consumer does not request or create a key.

```sh
python3 scripts/lease_history/report_v2.py \
  --base-url https://lockbot.example.invalid \
  --bot-id 7 --key-file /private/path/bot-op.key \
  --since 2026-01-01T08:00:00Z --until 2026-01-01T09:00:00Z \
  --resource node-a:dev0 \
  --max-pages 20 --page-size 25 \
  --save-export /private/path/new-lifecycle-export.json
```

Use the exact origin you trust, without a path, URL credentials, query or fragment. Prefer HTTPS; an explicitly selected HTTP origin transmits its bearer credential without TLS protection. Redirects are refused and credentials are never copied to a different target. Errors contain fixed summaries, not response bodies, headers or key values. No automatic request replay occurs, including on 401/403/409/429.

The window must be timezone-aware, ordered and at most seven days. `--resource` is an exact canonical resource ID; omit it for the whole bot. Node grants remain node units, and device grants remain device units. The consumer does not guess how many cards a node represents.

Collection keeps the origin, bot, window, resource and page size fixed, including for empty pages with a cursor. It pins the first `as_of` and frozen checkpoint across all pages. Requests are spaced by at least 2.1 seconds; defaults are 20 pages, at most 100, with bounded page/input/total bytes. A repeated cursor, changed scope/checkpoint, network failure or local budget ends the run without reporting complete totals. The source cursor additionally binds the current bot UUID/revision and OP credential on the server; the response does not expose a separate UUID proof that this client could verify.

`--save-export` creates a new mode-`0600` file and refuses to overwrite a path. The export can contain bot-scoped public actor IDs and resource facts, so keep it private to authorized readers. It contains no OP credential. A saved export is trusted local input, not independently signed evidence.

## Analyze a saved export

Pass the same bot/window/resource selected during collection:

```sh
python3 scripts/lease_history/report_v2.py \
  --input /private/path/new-lifecycle-export.json --bot-id 7 \
  --since 2026-01-01T08:00:00Z --until 2026-01-01T09:00:00Z \
  --resource node-a:dev0
```

A verified synthetic example can produce:

```json
{
  "coverage_complete": true,
  "complete_history": false,
  "subject_grant_seconds": {"device_exclusive_subject_grant_seconds": 30},
  "committed_events": {"acquired": 1, "renewed": 1, "released": 1}
}
```

These are subject-grant seconds inferred from committed allocator states, not measured utilization, physical occupancy, successful request percentages or productive work. Shared grants count each subject independently. Renewals become effective only at their committed frame, so a late renewal cannot backfill an expired interval. Event initiators are reported separately from allocation holders; event IDs and exact repeated frames are deduplicated, while conflicting evidence is refused. Public lease references and versions are correlation evidence, never authorization or holding proofs.

Complete totals require exhausted frozen pagination, the final server-verified window coverage, an atomic complete `coverage.start_anchor`, a consistent settled frozen checkpoint and valid scoped frame/event/version evidence. Filtered states retain whole-bot digests: the consumer must not recompute those digests from projected rows or reject omitted unrelated commits as lost evidence. It relies on the trusted server's unfiltered-chain coverage check, while validating the projection and fixed request scope locally.

Missing/legacy evidence, restart boundaries, unsettled checkpoints and truncation produce `coverage_complete: false`, explicit reasons and `subject_grant_seconds: null`; unknown is not zero. Exit status is 0 for verified coverage, 2 for incomplete evidence or invalid configuration/input. Full history is never certified because retention is finite.

Synthetic tests cover acquisition/renewal/release, filtered and unfiltered production-feed projections, frozen pagination, missing evidence, late renewal, conflicting identity, credential-file safety and fixed transport errors. They do not establish actual workload usage, a full live sequence on a deployed service, or authorize publishing a report to a group. See the [V2 contract](../design/verified-allocation-history.md) and open [usage analysis](https://github.com/dynamicheart/lockbot/issues/21) / [lifecycle acceptance](https://github.com/dynamicheart/lockbot/issues/54) work.
