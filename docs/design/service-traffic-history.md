# Historical service monitoring

Status: backend implementation for #146. This records service HTTP traffic and resource-operation attempts, **not hardware utilization**, detailed Agent notes or authorization audit evidence. Frontend delivery, current-head CI and 8002 acceptance are separate gates.

## Storage and request cost

The live 60-minute counters remain in memory. An additional bounded accumulator records only minute counters for the global scope and at most 256 recently observed bots. It retains fixed HTTP source categories, status classes, byte totals, summed latency and a fixed latency histogram; resource operations retain their source counts. Routes, raw URLs, query strings, caller identities, credential IDs, bodies and task details are never serialized.

Request handling only updates memory. A background thread wakes every 15 seconds, seals completed minutes and writes one bounded batch per hourly JSONL segment under the managed data directory's `monitor-history/`. Files are created with mode 0600, the directory with mode 0700. Records are versioned (`v:1`) and use UTC epoch minute timestamps, a random process identifier and explicit observed time intervals. Each durable batch is flushed and fsynced. Missing/unwritable/full storage affects monitoring only and emits a warning on transition into failure; resource requests continue.

A derived `hourly/` JSONL index merges each completed hour by scope. Counts and histogram buckets are added before estimating P95; minute P95 values are never averaged. The index preserves the union of actual observed intervals, including restart gaps. It is derived data, not a second database. Sealing examines at most the previous two hour segments, each at most 16 MiB; startup does not scan all history. Existing indexes survive restart. Missing older indexes after an extended outage remain unknown and are not rebuilt through unbounded startup work.

Minute detail is eligible for deletion after 25 hours only if its hourly index exists. Hourly records expire after 30 days. A shared 128 MiB disk budget may evict older files earlier; 30 days is a retention ceiling, not a storage-capacity guarantee. Cleanup first removes old minute files, then hourly files. Write failures and budget eviction leave unknown coverage. There is no backfill from cumulative live counters.

The writer retains at most two minutes of pending memory. A writer stalled longer than that loses old pending data instead of growing without bound. Graceful shutdown records the current partial minute; abrupt shutdown may lose the unflushed interval. Incomplete tails are truncated before the next append; completed malformed lines (including overly nested JSON) are skipped with `damaged_segments:true`, never interpreted as zeros. Hourly indexes retain a safe `damaged_segment` marker so compaction and subsequent restarts do not hide this diagnostic. Unexpected worker errors are isolated and retried on the next interval rather than terminating collection. Ordinary reads do not repair files or renew observations.

## HTTP interface

- `GET /api/admin/service-traffic/history`: current super administrator required; optional `bot_id`.
- `GET /api/bots/{bot_id}/service-traffic/history`: the same per-bot read authorization as the live service monitor. The server pins the scope; this route does not accept a different bot scope.
- Both require `start` and `end` RFC3339 timestamps with offsets. Ranges must be positive and at most 31 days, with no material future end. Responses are `Cache-Control: no-store`.
- `resolution_seconds` is optional: `60`, `900`, `3600`, `21600`. Automatic resolution is 900 for 24h, 3600 for 7d, otherwise 21600. Sub-hour resolution is limited to 24h. A query may produce at most 2880 points.
- Boundaries are rounded outward to the requested resolution. Returned `start`/`end` and `at` are explicit UTC-offset instants. UI display conversion must not change epoch arithmetic.

Each result contains `schema_version`, `start`, `end`, `resolution_seconds`, `captured_at`, `bot_id`, `totals`, `history`, `coverage`, and `storage`. `totals` uses the live `Counters.payload()` shape and sums **retained observed data only**. It is not an estimate of unknown intervals.

Each history item contains `at`, `coverage` (`complete`, `partial`, `unknown`), `observed`, `observed_seconds`, the live counter payload, fixed `sources` request counts and `operations` counts (`chat`, `agent`, `operator`). Unknown points have null counters and null source/operation maps, rather than fake zero traffic. Partial points contain only observed counts and must be marked as incomplete. A continuously observed idle interval has genuine zero counts. The current unfinished minute is normally not yet persisted.

`coverage` summarizes complete/partial/unknown bucket counts. `storage` reports `healthy`, `last_write_at`, safe `error`, `retention_days`, `disk_budget_bytes`, `query_truncated`, and `damaged_segments`. Errors contain no filesystem paths or exception details. Queries open only UTC hour segments in the requested range, prefer hourly indexes for coarse resolutions, and stop after at most 64 MiB of relevant segment bytes. Each line is bounded at 1 MiB. Incomplete or truncated queries retain explicit unknown coverage. No request scans every historical file or executes a query against the business SQL database.

## Measured workload and limits

Local development measurement (Python 3.12.13, 20,000 synthetic successful Agent requests, one bot, 100 request bytes and 1000 response bytes): existing live counters averaged 6.32 microseconds/request; live plus journal memory accumulation averaged 9.52 microseconds/request. This is a microbenchmark, not production latency or a guarantee. No journal file existed before flushing.

The measured serialized global+one-bot minute was 879 bytes. A global+100-bot minute with one request per bot was 41,519 bytes. For 100 bots with one request per bot per minute over a simulated hour (6,000 requests), minute segments occupied 2,503,563 bytes and the hourly index 45,155 bytes; reading the indexed hour took 1.17 ms locally. At that specific density, 25h of detail plus 30d of hourly indexes projects to roughly 95 MB before filesystem overhead; this is a measured-workload projection, not a universal bound. Hourly derived indexes reduce long-range query/storage volume by aggregating 60 minutes without discarding the latency histogram. Actual retention depends on active bot count, request numbers, interval fragmentation and disk budget; operators must inspect growth in their environment. The real-service acceptance must include a restart and hour boundary before formal rollout.

## Compatibility and remaining acceptance

No SQL migration, existing key changes, hardware polling, metrics credentials or new detailed-note access are introduced. Historical journals never replace security audit logs. Existing global and per-bot live endpoints remain unchanged. Current tests cover aggregation, histogram merging, restart gaps, partial shutdown, idle zeros, bounded cardinality, corrupt tails, hourly indexing, detail expiry, retention/disk pressure, concurrent operations, safe storage failure and HTTP authorization/scoping. A real 8002 restart/boundary acceptance and the historical UI remain explicit rollout gates.
