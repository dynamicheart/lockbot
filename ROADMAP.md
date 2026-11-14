# Roadmap

Updated 2026-11-05. [Issue #27](https://github.com/dynamicheart/lockbot/issues/27) tracks delivery. This page separates outstanding work from delivered features and retained historical evidence. An open issue does not necessarily mean its implementation is missing.

## Accepted release baseline

[v3.2.0](https://github.com/dynamicheart/lockbot/releases/tag/v3.2.0), commit `b43e2dc17c188b83cf4df8a0f3587158a91809fc`, was published and formally deployed on 2026-10-31. Artifact `13000176833` has SHA-256 `eb39fdf1b0efebb0818e5477fd45a184fdc2552102f998a0487b18eeb8429436`. Preservation checks retained 39 bots, 26 running bots, 15 bindings, original configuration and keys, active locks and queues, applications and routes. These are dated observations, not a claim of a fresh production check on every documentation update.

Post-upgrade checks established:

- Ordinary owner/OP outputs excluded detailed Agent notes; ordinary explicit opt-in did not grant access. Four public task categories matched the authorized detailed history, which retained Agent labels.
- Metrics returned 16 hosts / 64 cards / 640 series, with cache and available-memory gauges for all 16 hosts.
- The credential registry held 38 Infoflow and 1 Feishu schema-1 records and passed its quick check. Anonymous resource reads returned 401 with `no-store`.
- One application receiver process completed its WebSocket handshake. No new formal-group resource commands or delegated-read burst ran during the upgrade; authenticated formal UI acceptance is not claimed.

See the [release checklist](RELEASE_CHECKLIST.md), [release record](docs/releases/3.2.0.md), and [formal deployment evidence](https://github.com/dynamicheart/lockbot/issues/27#issuecomment-6530309410).

## 2026-11-14 integration and Skill update

PR #133 is merged and its bounded Skill-only patch is installed on the formal v3.2.0 base. An explicit deployment origin is embedded in copied Markdown and both ZIPs, with matching hashes and version metadata. PR #134 is merged and installed on 8002, which was restored with its encrypted connection files and four sessions preserved. Real Chrome confirmed five visible groups and separate login/report sources. These observations do not establish a full formal rollout of the personal dashboard or credential recovery. The proposed next release is documented in [3.3.0](docs/releases/3.3.0.md).

## 2026-11-14 delivery completion

Version 3.3.0 is published and formally deployed from `2576438692ee71c6e617e4a039a8f044eeaae0c5`. All release workflows passed and the saved image was verified on the deployment server. Existing keys and authority, bot configuration, live resource state, port, data volume and runtime keys were preserved; native recovery and the personal dashboard are enabled. See the [release record](docs/releases/3.3.0.md).

The user accepted 8002 integration verification and chose direct formal upgrade without repeated manual testing. #57's implementation, migration and preservation scope is accepted; its extra fresh formal normal-delivery check is waived for this rollout. This is not new formal message-delivery evidence. No group command or allocation mutation was sent. #27 closes with this accepted delivery scope; #88/#120/#121 retain the explicit deferrals below.

## Completed acceptance on 2026-11-05

[#54 lifecycle audit](https://github.com/dynamicheart/lockbot/issues/54) is closed. The installed 8002 fictional DEVICE fixture produced acquisition, renewal and release; four frozen pages and the V2 consumer verified its non-empty atomic-anchor window. A separate stopped disposable fixture exercised three empty continuation pages, retained-file rotation and evicted-file rejection through the deployed endpoint. Missing checkpoints remained unknown; consumer negative tests rejected altered checkpoints from the real export. Temporary OP keys, the disposable row and its synthetic journal were removed; existing bot rows and journal hashes were preserved. These tests sent no real-group messages and did not modify 8155. [Completion evidence](https://github.com/dynamicheart/lockbot/issues/54#issuecomment-6651196591).

[#63 Feishu membership](https://github.com/dynamicheart/lockbot/issues/63) is also closed. The authorized Baozipu test application received actual removal/re-addition events in a fresh loopback runtime with zero resource bots/routes. The existing disposable group visibly received unbound onboarding with the configured backend URL and its Chat ID. The greeting policy was restored off, the temporary SDK/server stopped and copied secrets removed; the original application/group membership and active 8002 service were preserved. A fresh 62-test membership/unbound run passed. No corporate application permissions or 8155 configuration changed. [Evidence](https://github.com/dynamicheart/lockbot/issues/63#issuecomment-6651556576).

The verified interval certifies subject-grant allocation time for its selected window, not full retained history, hardware utilization or productive work. The synthetic boundary fixture is not presented as a real workload sequence. [#21 usage analysis](https://github.com/dynamicheart/lockbot/issues/21) is closed after the user reviewed the exact fictional sample and disposable target group; the Baozipu test application sent it through its existing schema-1 registry and dedicated proxy, and the group UI visibly displayed it. No real user ranking, detailed notes or automatic publication was enabled. [Report evidence](https://github.com/dynamicheart/lockbot/issues/21#issuecomment-6651657640). Future group reports retain their own content/target authorization.

## Post-release changes

These changes were merged after v3.2.0 and are now included in the installed v3.3.0 image:

- [PR #124](https://github.com/dynamicheart/lockbot/pull/124), commit `0d63c1b86b9e0051c9567dc287c9b5c1fa4bb34e`, adds the opt-in personal resource dashboard. Its final head passed all six CI checks; scoped session/provider regressions and integration frontend interactions passed. This does not establish a production rollout or an organization directory integration.
- [PR #126](https://github.com/dynamicheart/lockbot/pull/126), commit `2906c5ff74593f468c62870405431e104ff77bcb`, fixes monitoring bar alignment and credential caller-type labels. All six final-head checks passed and the integration assets match the build. Authenticated administrator interaction remains unverified.
- [PR #128](https://github.com/dynamicheart/lockbot/pull/128), commit `efd1ac8fb5e8e8a6bfa4b465bf68a50fe16615b3`, adds per-bot key discovery/recovery within a verified identity namespace, additive encrypted recovery copies and ordered Skill upgrade guidance. Feishu application/tenant isolation and changes during decryption have regressions; 163 focused checks, 156 compatibility checks and all six final-head CI checks passed. Installed integration acceptance then verified the additive migration, two legacy fixture bindings, per-bot recovery and retention of four browser sessions. One fictional fixture key was rotated during acceptance cleanup; external source credentials were unchanged. These changes were subsequently installed formally in 3.3.0; the earlier integration observations retain their stated scope.
- [PR #127](https://github.com/dynamicheart/lockbot/pull/127) freezes the queue-display test clock. Its 52 local queue checks and all six final-head CI checks passed. Production logic was unchanged.

Future status changes belong in [issue #27](https://github.com/dynamicheart/lockbot/issues/27) and this section, without rewriting the historical release evidence. Source merge, integration deployment and formal rollout are separate milestones.

## Unified resource Skill follow-up

[#164](https://github.com/dynamicheart/lockbot/issues/164) keeps one `lockbot-resource-agent` package for native connections and the optional personal dashboard. A private registry is a distribution channel for that same package. Private deployment adapters stay outside the public Skill. See the [design and current/proposed boundary](docs/design/unified-resource-agent-skill.md).

- [x] Inventory existing deployment-side identity provider, read-only reporters and advisory identity-directory hooks.
- [ ] Define authenticated, caller-scoped extension discovery and report reads; current browser reporters do not provide this Agent protocol.
- [ ] Separate portable package integrity from per-service rendered revisions and privately stored connection metadata.
- [ ] Extend the existing Skill/helper only for explicitly supported protocols; preserve per-instance/per-bot key scope and read-only limits.
- [ ] Validate native multi-service connections, private distribution, optional reporter isolation and independent fresh sessions before rollout.

## Explicitly deferred

- [#88 Infoflow → Feishu migration](https://github.com/dynamicheart/lockbot/issues/88): next-release work. The user chose to clear runtime state while preserving resource configuration and immutable history; carrying active locks, queues or paired cross-platform user identities is not the accepted scope. Read-only preflight is delivered; cutover execution is unmerged. Preserve the existing draft branches rather than reintroducing the discarded active-carry design.
- [#120 transport/allocator separation](https://github.com/dynamicheart/lockbot/issues/120) and [#121 CLI/SDK-assisted onboarding](https://github.com/dynamicheart/lockbot/issues/121) are future evaluations, not unfinished 3.2.0 release gates.
- H20/bastion VPN or IT network integration was explicitly deferred. Broader hardware/workload benchmarks and arbitrary external-client adoption are not established by the existing tests.
- Inter-Agent contact registration remains an undecided product idea; it is not an implicit task-note feature.

## Delivered scope

- Agent binding and key-only bot discovery, versioned Skill/manifest hints, multi-service connection records, lease expiry and privacy guidance. Server hints cannot compel an arbitrary external Agent to update.
- Ordinary OP resource/history access and separate super-administrator detailed-note delegation, expiry/rotation/revocation and explicit audited viewing. Ordinary OP keys do not inherit detailed-note access. [#80](https://github.com/dynamicheart/lockbot/issues/80) is closed after reconciling durable five-minute summary tests, actual integration persistence/UI evidence and the 3.2.0 rollout. A fresh 72-test window/delegation run passed on 2026-11-05; no fresh formal burst is claimed.
- Public task categories, private detailed remarks and Agent labels, retained JSONL history, permission projections and route/filter isolation for logs and downloads.
- Reliable trailing notification batches after bursts, with recorded real test-group acceptance; pending batches remain in-memory best effort, and uncertain sends are not replayed.
- Protected short resource links, visible reservation queues, ordered per-device utilization, host/cache/memory/process metrics, independent data-source grants and reproducible telemetry bundles. Default collection remains staggered at 300 seconds; browser/Agent connectivity is best effort.
- QUEUE_DEVICE, optional contiguous-device display merging, preview/model consistency, renewal prohibition, per-person resource caps and removal of standalone mode.
- Feishu and Infoflow integration, platform-specific identities, application management, dedicated Feishu proxy, group/name rendering, rich-text error feedback, site/platform settings and broadcast queue UI.
- Default-off Feishu reactions and in-place query panels passed real integration command acceptance; membership acceptance completed the real unbound-group greeting on 2026-11-05. Test-enterprise evidence does not approve another tenant's permissions.
- Agent-assisted deployment/onboarding documentation, read-only preflight and hosting evaluation. These guides do not establish one-click hosted deployment.

## Operations and boundaries

The integration service and temporary local acceptance servers were stopped after testing on 2026-10-31; the formal service and independent telemetry were retained. Starting a test receiver again requires checking application identity and avoiding duplicate receivers. This is a historical cleanup observation. On 2026-11-05, 8002 was observed running a newer personal-resource-panel integration service; the old nightly controller still reported stopped. Monitor #126 static files were updated on the actual active service without restarting its backend. Future operations must identify the listening process and directory rather than assuming the old runtime owns the port.

`main` is the protected default development branch; `release/2.x` is historical and previous main history is retained in `archive/main-before-v3-20261029`. Review current-head CI before squash merging. Upgrades preserve original data/keys, but database readability alone does not make reverting old privacy behavior safe.

Only Infoflow and Feishu are scheduled. Do not add a shared cross-platform resource pool, private-chat workflows, or unrelated permissions as hidden dependencies. Group publication, corporate permission expansion and destructive app deletion retain their specific authorization boundaries.

For detailed original requirements and dated test/PR evidence, use the [requirement coverage matrix](docs/design/requirement-coverage.md) and [historical roadmap](docs/releases/roadmap-through-3.2.0.md). Historical wording is preserved for traceability, not copied into the current TODO list.
