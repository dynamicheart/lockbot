# Roadmap

Updated 2026-11-14. [Issue #139](https://github.com/dynamicheart/lockbot/issues/139) tracks current follow-up delivery; #27 records the accepted release baseline. This page separates outstanding work from delivered features and retained historical evidence. An open issue does not necessarily mean its implementation is missing.

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

Future status changes belong in [issue #139](https://github.com/dynamicheart/lockbot/issues/139) and this section, without rewriting the historical release evidence. Source merge, integration deployment and formal rollout are separate milestones.

## Current follow-up checklist

[#139](https://github.com/dynamicheart/lockbot/issues/139) is the delivery checklist. Source merge, test-service acceptance and formal rollout are separate gates; the v3.3.0 production baseline above remains unchanged by a test preview.

### Integrated test preview

On 2026-11-14 the test service was updated to integration commit `98b8a03ee2a349c1e26ba5c44b95565c0db4f18d`, followed by the static-only connection layout update `cd890a4`. Backend verification passed 2,684 tests with 7 skips. Frontend verification passed 352 tests in UTC, lint and production build. Deployment checks preserved existing accounts, ownership, credential authority and personal-panel browser sessions. The live index matches the updated build. Chrome verified the scoped connection row before the layout update was deployed. Visual and clipboard verification on the updated page remain open.

| Requirement | Source and tracking | Test preview | Formal rollout |
| --- | --- | --- | --- |
| Inherited resource-link lifetime, 1–30 days or permanent, bot override and regeneration | #137; #145 merged | Installed; lifetime choices and existing links checked | Pending |
| Compact shared resource table and subdued empty queue labels in shared/admin views | #140 and #152 merged | Installed; administrator resource table checked | Pending |
| Datasource protocol, setup Skill and reproducible authorization/configuration guidance | #144 and #149 merged; #147 tracks further acceptance | Installed; template, mapping fields and guide links checked | Pending |
| Bounded JSONL service history and scoped historical UI | #146; #150 and #148 merged | Installed; history API, no-store, time ranges and unknown coverage checked | Pending |
| Continuous metrics grants and expiry recovery | #151; #162 under review | Installed; deadline/cancellation/renewal regressions passed; no new live aging claim | Pending |
| Complete Skill ZIP, scoped helper and concise promotion | #153; #158 merged; #161 under review | Installed; actual ZIP has Skill, helper and installation stamp, with actual deployment origin | Pending |
| Legacy key health and recovery guidance without automatic rotation | #156; #159 merged | Installed; existing keys retained | Pending |
| Independent administrator API credential page and compact issuance dialog | #155; #166 merged | Installed; real page/dialog checked without issuing a credential | Pending |
| Agent alias readback with its separate observation time | #167; #168 merged | Installed; 108 relevant API checks passed | Pending |
| Viewer timezone for resource deadlines and monitoring | #171 under review | Installed; browser timezone governs display and lease arithmetic uses the UTC instant | Pending |
| Scoped connection metadata, copyable Agent handoff and compact layout | #164; #172, #174 and #176 under review | Installed; the existing browser session showed an authorized native connection and the external report groups | Pending |
| One-time native key viewing with fresh original-key proof | #164; #173 under review | Installed; native behavior covered with test credentials. The preview's remote provider exposes connection metadata only | Pending |
| Separately confirmed native key replacement | #164; #175 under review | Independent source review and focused tests passed. Preview deployment and end-to-end acceptance remain open | Pending |

Do not backfill pre-installation historical gaps with zero traffic. The [metrics plugin protocol v1](docs/design/metrics-datasource-protocol.md) is vendor-neutral and includes Prometheus and HTTP JSON examples; a generic JSON adapter or personal-dashboard metrics attachment is not implemented merely by publishing that document.

The current integration preview, `0a05c45`, includes the #177 resource-link and account-session choices. Authenticated settings checks confirmed a permanent default for unset resource-link policies and a 90-day account-session default. The update preserved active bindings and browser sessions.

### Remaining Agent and personal-workspace work

- [ ] **One user-facing resource Skill:** #164 and the design in #165 retain `lockbot-resource-agent` as the sole user entry. Multiple services/groups use scoped connection/key references. Deployment-specific private adapters and registry details stay outside the public repository. #165 specifies the versioned discovery and read contract. #178 covers portable packages across approved services. #179 covers the restricted read bridge. Real loopback HTTP verified ZIP installation, a fresh helper process, four synthetic groups through a configured adapter, denial for a different binding, and unknown results after simulated upstream revocation. The current preview uses a remote identity provider, so the native Agent report endpoint is disabled there. A fresh Codex CLI process discovered the verified Skill package installed in an isolated project. Protected merges, interactive browser handoff and private distribution remain pending.
- [ ] **Fresh Agent and operational acceptance:** #154 and #163 track installation persistence, correct key recovery, personal-dashboard handoff/bookmark, alias read/write/clear, early-release polling, expiry and previous-Skill update behavior. Offline fixture checks, model observations, native-client installation and actual browser consent must be reported separately. Model results belong to the exact Skill/PR snapshot, not a permanent leaderboard.
- [ ] **Progressive personal resources and task context:** #180 tracks the staged resource counts and My Credentials tab in #181, and the original-binding task projection in #182. Actual loopback HTTP delivered a fast group before a delayed source and suppressed further batches after revocation. The 8002 integration build includes the stream. The high-volume preview uses fictional metrics, containers and task information. Complete protected merges, visual checks and scoped utilization integration.
- [ ] **Personal workspace connection management:** #172/#174/#176 implement scoped status and copyable Skill/service/bot/identity instructions. Refresh rechecks current authorization. Finish current-build browser/clipboard verification and protected merges.
- [ ] **Explicit key inspection/copy:** #173 implements short-lived authorization and explicit consent for the exact verified native binding. Finish protected merge and isolated native-browser acceptance with synthetic credentials. Legacy digest-only bindings require a valid original key for the same-value recovery flow in #156.
- [ ] **Separately confirmed key replacement:** #175 requires fresh original-key proof, the exact bot/identity scope, an impact preview, a typed group name and explicit second confirmation. Finish protected merge and isolated native-browser acceptance before enabling the opt-in capability for a deployment.

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
