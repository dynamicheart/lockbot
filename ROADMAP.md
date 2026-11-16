# Roadmap

Updated 2026-11-16. [Issue #139](https://github.com/dynamicheart/lockbot/issues/139) tracks current follow-up delivery; #27 records the accepted release baseline. This page separates outstanding work from delivered features and retained historical evidence. An open issue does not necessarily mean its implementation is missing.

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

On 2026-11-14 the test service was updated to integration commit `98b8a03ee2a349c1e26ba5c44b95565c0db4f18d`, followed by the static-only connection layout update `cd890a4`. Backend verification passed 2,684 tests with 7 skips. Frontend verification passed 352 tests in UTC, lint and production build. Deployment checks preserved existing accounts, ownership, credential authority and personal-panel browser sessions. At that point, the served index matched the updated build. Chrome verified the scoped connection row before the layout update. Later preview checks are recorded in [#139](https://github.com/dynamicheart/lockbot/issues/139).

| Requirement | Source and tracking | Test preview | Formal rollout |
| --- | --- | --- | --- |
| Inherited resource-link lifetime, preset durations or permanent, bot override and regeneration | #137; #145 and #177 merged | Installed; lifetime choices and existing links checked | Pending |
| Compact shared resource table and subdued empty queue labels in shared/admin views | #140 and #152 merged | Installed; administrator resource table checked | Pending |
| Datasource protocol, setup Skill and reproducible authorization/configuration guidance | #144 and #149 merged; #147 closed | Installed; the browser queried an independent signed test gateway using the setup handoff | Pending |
| Bounded JSONL service history and scoped historical UI | #146; #150, #148 and #185 merged | Installed; history API, no-store, time ranges and unknown coverage checked | Pending |
| Continuous metrics grants and expiry recovery | #151; #162 merged | Installed; 8002 issued scoped grants backed by an authorized test collector. Real workload acceptance remains below | Pending |
| Complete Skill ZIP, scoped helper and concise promotion | #153; #158 and #161 merged | Installed; actual ZIP has Skill, helper and installation stamp, with actual deployment origin | Pending |
| Legacy key health and recovery guidance without automatic rotation | #156; #159 merged | Installed; existing keys retained | Pending |
| Independent administrator API credential page and compact issuance dialog | #155; #166 merged | Installed; real page/dialog checked without issuing a credential | Pending |
| Agent alias readback with its separate observation time | #167; #168 and #188 merged | The 8002 ZIP passed alias read, set, change, clear and readback against a matching isolated API | Pending |
| Viewer timezone for resource deadlines and monitoring | #171 merged | Installed; browser timezone governs display and lease arithmetic uses the UTC instant | Pending |
| Scoped connection metadata, copyable Agent handoff and compact layout | #164; #172, #174 and #176 merged | Installed; the existing browser session showed an authorized native connection and the external report groups | Pending |
| One-time native key viewing with fresh original-key proof | #164; #173 merged | Installed; native behavior covered with test credentials. The preview's remote provider exposes connection metadata only | Pending |
| Separately confirmed native key replacement | #164; #175 merged | Independent source review and focused tests passed. Native browser acceptance remains open | Pending |

Do not backfill pre-installation historical gaps with zero traffic. The [metrics plugin protocol v1](docs/design/metrics-datasource-protocol.md) defines a vendor-neutral boundary. The native dashboard supports scoped Prometheus metrics. A generic HTTP JSON adapter remains a design item.

The earlier integration preview at `0a05c45` verified the #177 resource-link and account-session choices. Authenticated settings checks confirmed a permanent default for unset resource-link policies and a 90-day account-session default. The update preserved active bindings and browser sessions.

### Workbench and CLI acceptance

The combined 8002 candidate passed real browser consent with one authorized native group and four external read-only groups. Reload restored the fixed workbench entry. Only the native connection appeared under My Credentials. The real reporter had no current personal holding, so this run could not verify utilization on a held resource. An isolated native fixture separately verified two-group selection, exact scope, and the one-key handoff. [Preview evidence in #139](https://github.com/dynamicheart/lockbot/issues/139), [scoped report acceptance in #164](https://github.com/dynamicheart/lockbot/issues/164).

The CLI-first preview serves portable Skill revision `4751a14ff82fc001` and deployment-rendered revision `ca8bd3984a8f30ca`. Verified the hosted seven-entry ZIP, all 21 frontend files, and fresh installation and discovery. Existing bindings and browser sessions were preserved, and the Feishu receiver reconnected. A fresh Codex CLI process using this ZIP found its project-scoped test connection and read verified context and resources from an isolated service. It made no writes and left the Key and connection files intact. [CLI candidate and test results](https://github.com/dynamicheart/lockbot/pull/250), [Agent acceptance #154](https://github.com/dynamicheart/lockbot/issues/154), [alias acceptance #167](https://github.com/dynamicheart/lockbot/issues/167).

Eight models reran the three Key workflows with the exact hosted 8002 ZIP and simulated chat confirmation. Recovery passed 6/8, first creation 6/8 and explicit reset 4/8. The other eight cases timed out. [Chinese regression matrix](https://github.com/dynamicheart/lockbot/pull/250).

- [ ] Complete the manual 8002 walkthrough. Review the first-contact wording, install the hosted ZIP, bind in the test group, query from a fresh Agent session, then open the private sign-in link and confirm in the browser. Refresh the fixed workbench entry to verify it stays signed in. [#250](https://github.com/dynamicheart/lockbot/pull/250), [#139](https://github.com/dynamicheart/lockbot/issues/139).
- [ ] Use one real 8002 Key to recover multiple native connections for the same verified identity. The isolated bind flow recovered the original Key when active and issued a Key when none was active. An explicit reset used its own confirmation and invalidated the old Key. Complete live inspection and replacement with an authorized test binding. [#250](https://github.com/dynamicheart/lockbot/pull/250), [#173](https://github.com/dynamicheart/lockbot/pull/173), [#175](https://github.com/dynamicheart/lockbot/pull/175).
- [ ] Verify a fresh utilization sample and raw device/process scope on an authorized holding using the shared 8002 browser. A separate gateway using the real collector returned 20 whole-node process rows and five rows within each of two individual card grants. The shared gateway rejected the 8002 browser origin, and the authorized external report had no live holding during its separate check. [#180](https://github.com/dynamicheart/lockbot/issues/180), [#259](https://github.com/dynamicheart/lockbot/pull/259).
- [ ] Complete cross-machine private credential transfer and private registry distribution. The portable candidate was packaged and verified locally. Registry installation and upload require the user's explicit approval after the manual walkthrough. [#164](https://github.com/dynamicheart/lockbot/issues/164).

Configuration credential controls and the audit tab restriction were merged in [#253](https://github.com/dynamicheart/lockbot/pull/253). The authorization tests covered listing, issuance, rotation and revocation for ordinary users and administrators.

Workbench localization was merged in [#255](https://github.com/dynamicheart/lockbot/pull/255) after all six required checks passed on the updated branch.

### Open reviews

| Scope | Source | Evidence still needed |
| --- | --- | --- |
| CLI-first resource Skill and browser handoff | [#250](https://github.com/dynamicheart/lockbot/pull/250) | Real-account walkthrough and release review |
| Illustrated first-run setup | [#254](https://github.com/dynamicheart/lockbot/pull/254) | Authorized Infoflow setup screenshot and a clean-install group walkthrough. Earlier live Feishu command delivery used the configured 8002 receiver. |
| Whole-machine accelerator inventory | [#257](https://github.com/dynamicheart/lockbot/pull/257) | Live Agent selection. Synthetic models and backend tests covered known versus unknown hardware, and identical node IDs in separate groups. |
| Feishu application onboarding | [#121](https://github.com/dynamicheart/lockbot/issues/121) | Design for Site Settings, available to LockBot super administrators. The pinned Python SDK supports device-flow app registration. Verify creation in an authorized test enterprise and bind one test chat after publication and receiver checks. The standalone CLI draft [#258](https://github.com/dynamicheart/lockbot/pull/258) stays out of the merge queue. |
| Scoped reporter utilization | [#259](https://github.com/dynamicheart/lockbot/pull/259) | Authorized live sample described above. The narrow-screen layout update passed local Chrome checks. |
| Public API reference and maintenance guidance | [#260](https://github.com/dynamicheart/lockbot/pull/260) | The candidate removes the operator and datasource setup Skills. The administration guide links to the existing operator API and the datasource generator. 146 targeted tests passed. Recheck required CI against current main before merging. |

## Unified resource Skill follow-up

[#164](https://github.com/dynamicheart/lockbot/issues/164) keeps one `lockbot-resource-agent` package for native connections and the optional personal dashboard. The public package supports scoped extension discovery, caller-authorized report reads, and portable integrity checks. The implementation and configuration of private adapters remain outside the public repository. The isolated multi-service regression exercised distinct origins, instances, groups, and credential references with independent fresh sessions. The real-account and distribution items are tracked above. [Design and protocol boundaries](docs/design/unified-resource-agent-skill.md).

## Explicitly deferred

- [#88 Infoflow → Feishu migration](https://github.com/dynamicheart/lockbot/issues/88): next-release work. The user chose to clear runtime state while preserving resource configuration and immutable history; carrying active locks, queues or paired cross-platform user identities is not the accepted scope. Read-only preflight is delivered; cutover execution is unmerged. Preserve the existing draft branches rather than reintroducing the discarded active-carry design.
- [#120 transport/allocator separation](https://github.com/dynamicheart/lockbot/issues/120) remains a future evaluation.
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
