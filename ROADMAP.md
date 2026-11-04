# Roadmap

Updated 2026-11-04. [Issue #27](https://github.com/dynamicheart/lockbot/issues/27) tracks delivery. This page separates outstanding work from delivered features and retained historical evidence. An open issue does not necessarily mean its implementation is missing.

## Accepted release baseline

[v3.2.0](https://github.com/dynamicheart/lockbot/releases/tag/v3.2.0), commit `b43e2dc17c188b83cf4df8a0f3587158a91809fc`, was published and formally deployed on 2026-10-31. Artifact `13000176833` has SHA-256 `eb39fdf1b0efebb0818e5477fd45a184fdc2552102f998a0487b18eeb8429436`. Preservation checks retained 39 bots, 26 running bots, 15 bindings, original configuration and keys, active locks and queues, applications and routes. These are dated observations, not a claim of a fresh production check on every documentation update.

Post-upgrade checks established:

- Ordinary owner/OP outputs excluded detailed Agent notes; ordinary explicit opt-in did not grant access. Four public task categories matched the authorized detailed history, which retained Agent labels.
- Metrics returned 16 hosts / 64 cards / 640 series, with cache and available-memory gauges for all 16 hosts.
- The credential registry held 38 Infoflow and 1 Feishu schema-1 records and passed its quick check. Anonymous resource reads returned 401 with `no-store`.
- One application receiver process completed its WebSocket handshake. No new formal-group resource commands or delegated-read burst ran during the upgrade; authenticated formal UI acceptance is not claimed.

See the [release checklist](RELEASE_CHECKLIST.md), [release record](docs/releases/3.2.0.md), and [formal deployment evidence](https://github.com/dynamicheart/lockbot/issues/27#issuecomment-6530309410).

## Remaining work

| Issue | Delivered evidence | Remaining action |
| --- | --- | --- |
| [#54 lifecycle audit](https://github.com/dynamicheart/lockbot/issues/54) | Contract and V2 consumer are merged and released. Isolated HTTP/CLI acceptance covers acquisition, renewal, booking, release, expiry, frozen pagination and restart-to-unknown. Installed integration acceptance exercised six commands, eight committed events and nine frozen pages; its initial non-atomic boundary correctly produced unknown coverage and null totals. | Verify the remaining installed-service export guarantees and a report window with a valid initial anchor. Do not infer complete retained history or resource-hours from the existing unknown-boundary sample. [Integration evidence](https://github.com/dynamicheart/lockbot/issues/54#issuecomment-6527870715). |
| [#21 usage analysis](https://github.com/dynamicheart/lockbot/issues/21) | Public OP request counts and bounded lifecycle reporting are available. | Build further explainable analysis from verified lifecycle evidence after #54; allocation time, measured hardware use and productive work remain distinct. No automatic leaderboard publication is authorized. |
| [#57 messaging credential registry](https://github.com/dynamicheart/lockbot/issues/57) | Versioned provider-specific registry, additive/idempotent migration, legacy fallback, encrypted-key continuity and guards are merged. Clone tests, integration restart/delivery and formal registry/preservation checks passed. | Finish the remaining normal-delivery/restart rollout acceptance for the installed formal credential path; do not repeat the implementation or treat the old pre-release issue text as current deployment status. |
| [#80 delegated-read summaries](https://github.com/dynamicheart/lockbot/issues/80) | Durable five-minute aggregation, UI grouping and credential controls are merged and released. Actual integration reads/denials produced exact counters; restart retained buckets, and temporary credentials were revoked. | Reconcile the issue's rollout gate with the accepted release evidence and retain the unperformed fresh formal burst as an explicit limitation. [Acceptance evidence](https://github.com/dynamicheart/lockbot/issues/80#issuecomment-6521208966). |
| [#63 Feishu membership](https://github.com/dynamicheart/lockbot/issues/63) | Optional default-off membership handling is released. Actual removal/re-addition of a bound disposable test group passed; warning cleared, route/resources stayed intact and options were restored off. | Complete real unbound-group greeting acceptance. The earlier locked-client attempt was superseded by the successful bound-group run. [Evidence](https://github.com/dynamicheart/lockbot/issues/63#issuecomment-6529107006). |
| [#27 overall delivery](https://github.com/dynamicheart/lockbot/issues/27) | The accepted release and original delivered scopes are recorded below. | Keep the outstanding acceptance items above visible and close completed issue scope only against its own requirements. |

[PR #124](https://github.com/dynamicheart/lockbot/pull/124) adds a personal resource dashboard. It is open at this snapshot, is separate from the 3.2.0 release, and is not treated as accepted or deployed here.

## Explicitly deferred

- [#88 Infoflow → Feishu migration](https://github.com/dynamicheart/lockbot/issues/88): next-release work. The user chose to clear runtime state while preserving resource configuration and immutable history; carrying active locks, queues or paired cross-platform user identities is not the accepted scope. Read-only preflight is delivered; cutover execution is unmerged. Preserve the existing draft branches rather than reintroducing the discarded active-carry design.
- [#120 transport/allocator separation](https://github.com/dynamicheart/lockbot/issues/120) and [#121 CLI/SDK-assisted onboarding](https://github.com/dynamicheart/lockbot/issues/121) are future evaluations, not unfinished 3.2.0 release gates.
- H20/bastion VPN or IT network integration was explicitly deferred. Broader hardware/workload benchmarks and arbitrary external-client adoption are not established by the existing tests.
- Inter-Agent contact registration remains an undecided product idea; it is not an implicit task-note feature.

## Delivered scope

- Agent binding and key-only bot discovery, versioned Skill/manifest hints, multi-service connection records, lease expiry and privacy guidance. Server hints cannot compel an arbitrary external Agent to update.
- Ordinary OP resource/history access and separate super-administrator detailed-note delegation, expiry/rotation/revocation and explicit audited viewing. Ordinary OP keys do not inherit detailed-note access.
- Public task categories, private detailed remarks and Agent labels, retained JSONL history, permission projections and route/filter isolation for logs and downloads.
- Reliable trailing notification batches after bursts, with recorded real test-group acceptance; pending batches remain in-memory best effort, and uncertain sends are not replayed.
- Protected short resource links, visible reservation queues, ordered per-device utilization, host/cache/memory/process metrics, independent data-source grants and reproducible telemetry bundles. Default collection remains staggered at 300 seconds; browser/Agent connectivity is best effort.
- QUEUE_DEVICE, optional contiguous-device display merging, preview/model consistency, renewal prohibition, per-person resource caps and removal of standalone mode.
- Feishu and Infoflow integration, platform-specific identities, application management, dedicated Feishu proxy, group/name rendering, rich-text error feedback, site/platform settings and broadcast queue UI.
- Default-off Feishu reactions and in-place query panels passed real integration command acceptance; membership acceptance retains the unbound-group gap above. Test-enterprise evidence does not approve another tenant's permissions.
- Agent-assisted deployment/onboarding documentation, read-only preflight and hosting evaluation. These guides do not establish one-click hosted deployment.

## Operations and boundaries

The integration service and temporary local acceptance servers were stopped after testing on 2026-10-31; the formal service and independent telemetry were retained. Starting a test receiver again requires checking application identity and avoiding duplicate receivers. This is a cleanup observation, not an assertion that services cannot have changed since then.

`main` is the protected default development branch; `release/2.x` is historical and previous main history is retained in `archive/main-before-v3-20261029`. Review current-head CI before squash merging. Upgrades preserve original data/keys, but database readability alone does not make reverting old privacy behavior safe.

Only Infoflow and Feishu are scheduled. Do not add a shared cross-platform resource pool, private-chat workflows, or unrelated permissions as hidden dependencies. Group publication, corporate permission expansion and destructive app deletion retain their specific authorization boundaries.

For detailed original requirements and dated test/PR evidence, use the [requirement coverage matrix](docs/design/requirement-coverage.md) and [historical roadmap](docs/releases/roadmap-through-3.2.0.md). Historical wording is preserved for traceability, not copied into the current TODO list.
