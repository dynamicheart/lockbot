# Roadmap

Updated 2026-10-30. [Issue #27](https://github.com/dynamicheart/lockbot/issues/27) tracks execution; [requirement coverage](docs/design/requirement-coverage.md) maps the discussion to delivery and acceptance. **Merged, released, deployed and accepted are separate states.** The current sections supersede the folded historical snapshots below.

## Formal release and accepted deployment

**[v3.1.0](https://github.com/dynamicheart/lockbot/releases/tag/v3.1.0), release commit `8eba15c`, is published and formally deployed.** The deployment coordinator recorded preservation of 38 bots, 25 running bots and 14 Agent bindings, together with existing ports, keys, configuration, active locks and queues. These are preservation checks, not a list of private resource identities.

Accepted against the formal service:

- Dedicated, scoped `task_notes:read` delegation: authorized history/live-declaration reads, cross-bot and credential-family denial, rotation and revocation. Temporary acceptance credentials were revoked afterward. [Issue #56](https://github.com/dynamicheart/lockbot/issues/56) is **closed** for this delivered scope; see the [delegation design](docs/design/delegated-task-notes.md).
- Ordinary bot-owner and OP reads exclude detailed Agent notes. Default UI reads remain public; super-administrator detailed-note viewing is explicit and audited. An ordinary OP key, personal key or metrics grant never inherits delegated note authority.
- Independent metrics returned **16 hosts / 64 cards**, including cache and available-memory data for all hosts. The v3.1 browser check accepted the wide bot-detail layout and ordered device metrics. This does not establish mobile touch acceptance, NVIDIA fleet rollout or driver-lock performance impact.
- The standard-library OP history/report consumer read three pages / 150 rows. Its 24-hour sample contained **133 requests from 8 actors**: 69 lock, 14 book and 50 unlock, reported as `dispatched`. The export is **partial** and operation-only: no successful-lock rate, allocated card-hours or productive-work attribution is inferred. Detailed fields were absent and no group messages were sent. [Issue #21](https://github.com/dynamicheart/lockbot/issues/21) remains open for broader analysis/consumer scope.

The v3.1.0 release includes delegated note access and layout changes; it **does not include PR #70's lifecycle contract, PR #68's reactions, PR #71's membership notices, PR #72's queue panels or PR #74's process popover fix**. Later code on `main` is not automatically part of the installed image. The release notes' preparation-time production status is historical; the accepted deployment record above supersedes it.

`main` is the protected default development branch; new PRs target `main` and use current-head checks and squash merge. The prior main remains in `archive/main-before-v3-20261029`; `release/2.x` is historical. The 3.0.0 branch transition is complete, not a remaining TODO.

## Merged after the formal release: acceptance still required

| Scope | Code / review evidence | Next gate |
| --- | --- | --- |
| [#54 lifecycle audit contract](https://github.com/dynamicheart/lockbot/issues/54) | [PR #70](https://github.com/dynamicheart/lockbot/pull/70) merged as `4c6b2ca`; final head `ea1d097` passed six CI checks. Recovery-envelope bot UUID was fixed and tested. Validation: 1,851 core/backend passed / 7 skipped plus 36 integration/developer/report checks (1,887 passed total); independent review: 21 targeted checks, not added to that total. | Isolated loopback HTTP/socket smoke on `6b72d17` passed: four frames/five pages; lock/renew/book/free and OP denial; journal-failure/SIGKILL recovery exactly once with the same bot UUID; stable IDs through two restarts, with cross-restart coverage unknown. All outbound requests were blocked, platform delivery stubbed and Feishu SDK disabled; processes exited. [Sanitized evidence](https://github.com/dynamicheart/lockbot/issues/54#issuecomment-6506254246). This is **not integration-host or formal-service acceptance**. Relay authentication still gates live integration restart/export and later release/formal rollout. **Issue remains open.** |
| [#64 optional Feishu reactions](https://github.com/dynamicheart/lockbot/issues/64) | [PR #68](https://github.com/dynamicheart/lockbot/pull/68) merged as `b145bfa`; six checks on `aa9caeb`. Default off, Feishu only, committed short successes only; failures retain text fallback without replay. | Explicit user confirmation of the narrow reaction permission/publication and real rendering/fallback acceptance in the authorized **test enterprise only**. No corporate-tenant permission change. Issue was reopened after automatic merge closure; it is not delivered/accepted merely because code merged. |
| [#63 optional membership guidance/notices](https://github.com/dynamicheart/lockbot/issues/63) | [PR #71](https://github.com/dynamicheart/lockbot/pull/71) merged as `6b72d17`; reviewed head `2da0817` passed six checks. | Default-off behavior still needs affected live routing/removal/onboarding acceptance and a later release record. The issue was reopened after automatic merge closure; merge does not close its acceptance gate. |
| [#62 optional queue panels](https://github.com/dynamicheart/lockbot/issues/62) | [PR #72](https://github.com/dynamicheart/lockbot/pull/72) merged as `5133afe`; six checks on final `878e5ce`. Root independent panel/membership/reaction checks: 81 passed; author combined checks: 128, separate Infoflow/resource checks: 128 and frontend: 175 (overlapping suites, not summed). | Default off and **not deployed**. Real authorized-group update/fallback/isolation acceptance and a later release remain. Issue reopened after automatic merge closure. |
| [PR #74 process details layout](https://github.com/dynamicheart/lockbot/pull/74) | Merged as `585fbb4`; six checks on final `88a1f3a` ([CI](https://github.com/dynamicheart/lockbot/actions/runs/40939877857)), frontend 175/lint/build passed. Actual Vue rendering with fictitious data at 390px desktop-browser viewport showed the popover within x=12…378 and container text visible; the old formal layout's 520px left overflow was reproduced. | **Not deployed**. This is constrained viewport/fake-data evidence, not physical mobile/touch or real hardware validation. Keep #28's workload/touch/rollout boundaries. |

`main` includes PRs #70/#68/#71/#72/#74; these source merges do not change what is already deployed as v3.1.0. Evidence bounds and old/unsupported deployments remain explicit in the [lifecycle design](docs/design/verified-allocation-history.md) and OP Skill.

## Active work and review queue

| Issue / PR | Remaining scope and gate |
| --- | --- |
| [#21 usage analysis](https://github.com/dynamicheart/lockbot/issues/21) | Retain the accepted public OP request-report consumer evidence above. Complete broader report design separately from #54's live lifecycle acceptance. No automatic leaderboard or group publication. |
| [#28 telemetry operations](https://github.com/dynamicheart/lockbot/issues/28) | Broader NVIDIA deployment, measured SMI driver-lock/workload impact and mobile browser/touch acceptance. Preserve staggered five-minute sampling, cached queries and unknown/stale states. |
| [#57 / PR #73 messaging credentials](https://github.com/dynamicheart/lockbot/pull/73) | **Merged as `991bc5`** after six CI checks on final `566c289`, 2,061 tests passed / 7 skipped and 154 independent checks (not summed). Read-only formal-data clone preflight backfilled 38 bots / 14 bindings across 17 tables / 643 rows: all 38 configurations remained readable, repeat backfill changed zero rows, hashes were preserved, unknown schema failed closed, temporary data was cleaned and the live container was unchanged. **Not deployed and not a formal migration**; #57 remains open for rollout acceptance. Keep legacy compatibility and credential authorities separate. |

## Closed scope, deliberate boundaries and deferred decisions

- [#24](https://github.com/dynamicheart/lockbot/issues/24) is **closed for the accepted handoff regression**, after independent review of one real cooperating Codex parent → subagent → external HTTP run on an authorized fictional integration bot (service 3.0.2; no formal upgrade). The parent verified the same-origin Skill/hash, stripped child fields at actual command serialization, rejected a child policy expansion and verified owner/foreign-note/revision boundaries. Exact If-Match cleanup preserved the lock and stale reuse returned 412; release followed an actor/resource/start-time/expiry match. Feedback used the verified manifest Issues route, without sending an unsolicited report. The successful run used 23 HTTP requests. A prior over-polling attempt stopped on 429 without mutation replay; its short lease expired and idle was confirmed before a new run with repeated GETs at least ten seconds apart. This is not automatic installation/update, universal host adoption, a malicious-child sandbox or service-side atomic compare-and-free. Private scripts/raw proof are not published.
- [#33](https://github.com/dynamicheart/lockbot/issues/33) is **closed** for independent data-source signing and documented client/connectivity protocol boundaries. Client-local source tests cannot prove that every viewer can reach a source. The separate bastion/VPN/IT network experiment was **deferred by the user**; it is not unfinished #33 scope and must not be restarted as a hidden telemetry dependency.
- [#36 privacy](https://github.com/dynamicheart/lockbot/issues/36), [#39 trailing notifications](https://github.com/dynamicheart/lockbot/issues/39), [#29 Skill updates](https://github.com/dynamicheart/lockbot/issues/29) and [#60 category/layout regression](https://github.com/dynamicheart/lockbot/issues/60) are closed for their delivered scopes. Detailed-view categories use validated server labels; Agent/name and note lines are labeled separately. No promise of forced external-client compliance or durable notification delivery is implied.
- Support Infoflow and Feishu with one enterprise application serving explicitly associated groups and independent resource bots. Platform is fixed at creation; migration copies safe configuration into a new form. **No shared resource pool across platforms**, in-place platform switch, same-name identity merging or private-chat support is scheduled. Standalone was removed; Slack/DingTalk abstractions are not being revived.
- Application management, encrypted Secret rotation, explicit audited temporary Secret display, guarded deletion, proxy settings and name lookup are delivered foundations. Corporate-enterprise permission/publication decisions remain human gates; test-enterprise acceptance does not approve another enterprise. The [setup guide](docs/usage/feishu-setup.md) has six existing verified console clips; [PR #76](https://github.com/dynamicheart/lockbot/pull/76) adds a reviewed seventh native clip and explicit screenshot coverage (reviewed, subject to synchronized-head checks/merge). Version-form, member-range and new approval-flow images are still absent; seven verified images do not prove a fresh enterprise application or approval.
- Agent contact registration/inter-Agent communication was an early idea without an agreed implementation commitment; retain it as an open product decision, not an accidental extension of private task notes.
- Data-source queries are browser/Agent-direct and best effort, independent of allocator state. Metrics grants, CORS, network reachability and detailed-note delegation are separate. CPU available memory and buffers/cache do not diagnose a driver OOM by themselves; low utilization never authorizes reclaim or process termination.
- [Agent deployment/onboarding guides](docs/agents/AGENTS.md), read-only preflight and [managed-hosting guidance](docs/hosting/README.md) are delivered. Real provider deployment/one-click hosting is not accepted by documentation or Compose parsing alone. Deployment, permission expansion, credential reveal and group messages still require the user's concrete authorized scope.
- Upgrades preserve data/key/state compatibility; database readability does **not** make downgrading to old privacy behavior safe. See [upgrade/rollback](docs/design/upgrade-rollback.md). Keep release/tag, artifact checksum, deployment preservation and real acceptance records separate.

## Historical evidence appendix

<details>
<summary>Archived tracking before the accepted v3.1.0 rollout — superseded status wording</summary>

**Historical record only.** References below to “current”, “pending”, “unreleased”, an integration candidate, v2.15.2/v3.0.x production or a future main transition describe their earlier snapshots. They are not current TODOs. Use the current sections above for status; preserve these records for PR heads, test counts and earlier user requirements.

Updated 2026-10-30. [Roadmap issue #27](https://github.com/dynamicheart/lockbot/issues/27) tracks execution and [the requirement coverage matrix](docs/design/requirement-coverage.md) maps the discussion to evidence and remaining acceptance. Implementation, merge, release, deployment and acceptance are different states. An open PR or a successful local test is not a production rollout.

## Current delivery

- **3.0.2 is released and formally deployed.** [Current release](https://github.com/dynamicheart/lockbot/releases/tag/v3.0.2), commit `eccb4d1`, PRs #61/#65. Detailed-view public categories and Agent/note captions are corrected; the full-width bot page is retained. Production preservation, ordinary-owner/OP privacy projections and independent 16-host/64-card metrics passed.
- **3.0.0 initial rollout:** [Release](https://github.com/dynamicheart/lockbot/releases/tag/v3.0.0), accepted commit `80cc6a5`, PRs #25/#58. All required checks passed; full integration validation: 1,820 backend passed / 7 skipped and 170 frontend passed.
- The formal upgrade preserves original ports, data, keys, configuration, active locks, queues and bindings. Post-upgrade ordinary-owner and OP projections exclude other-user detailed notes. The independent source query returned 16 hosts, 64 cards and cache/available-memory metrics for all hosts.
- `main` is the default development branch, aligned to the accepted release commit at transition. The previous main is archived in `archive/main-before-v3-20261029`; `release/2.x` remains historical. Both main and the historical release branch prohibit force pushes/deletion and require the six CI checks.
- The current 3.0.2 image artifact was downloaded and verified: SHA-256 `cf9386a56ce02615dbe3472c2c79afe8511eb53ec398c41d516dacb9f10dfe6b` (artifact `12914489614`). Telemetry bundles for amd64/arm64 are release assets.
- **Corporate Feishu approval is pending human review.** Code availability does not create or approve an enterprise application. Keep test and production application identities separate.

## Remaining work

| Issue | Remaining scope |
| --- | --- |
| [#24](https://github.com/dynamicheart/lockbot/issues/24) | Actual external Agent adoption of handoff/version/privacy rules; server hints cannot compel arbitrary clients |
| [#21](https://github.com/dynamicheart/lockbot/issues/21) | Further explainable resource-use analysis; report only measured allocation evidence, not hardware work |
| [#28](https://github.com/dynamicheart/lockbot/issues/28) | Broader NVIDIA deployment and measured driver-lock/workload impact; retain five-minute collection defaults |
| [#33](https://github.com/dynamicheart/lockbot/issues/33) | Additional isolated-source trust/client connectivity rollout; the bastion/VPN experiment remains user-deferred |
| [#54](https://github.com/dynamicheart/lockbot/issues/54) | Durable lifecycle audit API contract and explicit coverage |
| [#56](https://github.com/dynamicheart/lockbot/issues/56) | Separately delegated super-administrator detailed-note credentials |
| [#63](https://github.com/dynamicheart/lockbot/issues/63) | Optional Feishu membership onboarding/removal notices; default off, isolated implementation/tests in progress, permission approval and real-group acceptance pending |
| [#62](https://github.com/dynamicheart/lockbot/issues/62) | Optional Feishu in-place queue panels; default off, permission failure retains normal replies; implementation in a separate PR |
| [#64](https://github.com/dynamicheart/lockbot/issues/64) | Optional Feishu success reactions in PR #68; default off, committed-result evidence and unchanged Infoflow/legacy messages; real rendering acceptance pending |
| [#57](https://github.com/dynamicheart/lockbot/issues/57) | Versioned messaging credentials and an additive legacy-compatible migration |

<details>
<summary>Historical delivery and acceptance snapshots before the 3.0.0 rollout</summary>

The following records preserve earlier implementation/release boundaries. Their pending rollout and branch-transition wording describes those historical snapshots; use the current delivery and remaining-work sections above for present status.

## Delivery snapshot

| State | Evidence and next gate |
| --- | --- |
| Formal deployment | **[v2.15.2](https://github.com/dynamicheart/lockbot/releases/tag/v2.15.2)**, release commit `f4badac`. The two P0 fixes are released: private-task role separation (#40) and trailing notification delivery (#42). Existing keys/configuration/locks/queues were preserved; actual formal OP/owner projection, Skill/version and direct-source 16-node/64-card checks passed. |
| Merged, outside formal release | Promotion #19, private history/scoped monitoring #22, authorization documentation #35, safe category validation #37, resource layout #38, per-source signers #41, client handoff #43, public OP history/report #44, log scope #46, detail route scope #47, conditional declaration cleanup #48, verified lifecycle accounting #49, audit action translation #50 (`88a9a11`), layered operation trends #51 (`4f76627`), explicit Agent-note opt-in #52 (`37bb316`), hosting guidance #55 (`445bea7`) and deployment/onboarding preflight #53. Each passed its final required checks before squash merge; PR/Issue records retain reviewed heads and test evidence. These are outside formal v2.15.2. |
| Lifecycle accounting candidate | [PR #49](https://github.com/dynamicheart/lockbot/pull/49): committed allocation evidence and verified interval reporting. Strict checkpoint validation fixes a reproduced false-coverage bug. Squash-merged as `ef00d76` after independent review, 114 frozen-candidate targeted checks and six final-head CI checks on `a6fe893`. Formal release remains required; real integration reports now verify complete current-process intervals and correctly mark cross-restart evidence gaps unknown. This measures grants, never hardware utilization or actual work. |
| Feishu integration | [PR #25](https://github.com/dynamicheart/lockbot/pull/25) squash-merged as `fa3fb08` after independent review, **1,820 backend tests / 7 skipped, 170 frontend tests** and all six required checks on final head `7d15888` ([CI](https://github.com/dynamicheart/lockbot/actions/runs/40906163694)). Integration now runs that reviewed head. Configuration/credentials/routes, group/name lookup, public-read auditing, broadcasts/dialogs and running/platform filters passed affected checks. Formal production remains v2.15.2; corporate application approval remains a separate human gate. |
| Real integration acceptance | Earlier real two-group routing/isolation acceptance passed on the tested integration candidate. Subsequent integration checks covered the revised administration UI, group/application names and receiver status; prior default-vs-explicit detailed-note auditing, conditional cleanup and lifecycle interval/gap evidence remain recorded. Do not imply that every later source change reran the full two-group/rendering matrix. Detailed member-list scope was not expanded; formal rollout and corporate application approval remain separate gates. |
| Remaining work | Real external-client adoption/task cleanup (#24), broader lifecycle/report rollout (#21), telemetry operational/performance acceptance (#28), and source-trust rollout boundaries (#33). The requirement coverage matrix distinguishes delivered behavior, missing evidence, limitations and user-deferred network work. |

Merge, tag/release, image/artifact build, deployment and real acceptance remain separate gates. Issues #36/#39 are closed for their delivered P0 scope; subsequent endpoints and candidates need their own checks.

## Priorities and dependency order

| Priority | Work | Delivery boundary and next gate |
| --- | --- | --- |
| P0 | Stop silently losing Agent resource notifications | [#39](https://github.com/dynamicheart/lockbot/issues/39), [PR #42](https://github.com/dynamicheart/lockbot/pull/42): released/deployed in v2.15.2. Real authorized test-group acceptance observed the trailing resource notice without a fourth request; pending batches remain in-memory best effort and uncertain sends are not retried |
| P0 | Separate private task access from bot administration and OP maintenance | [#36](https://github.com/dynamicheart/lockbot/issues/36), [PR #40](https://github.com/dynamicheart/lockbot/pull/40): squash-merged after fresh review and six green checks; retain own-user access and default other-user access to super administrators. Existing server projections, bundled Skills and metadata are released; actual formal OP/owner projection checks passed. Future history/search/export surfaces must reuse and separately validate this policy |
| P1 | Finish private history and scoped service monitoring | [PR #22](https://github.com/dynamicheart/lockbot/pull/22) merged as `d3c86e1` after six checks; 62 focused backend and 97 frontend tests passed. Current authority and ordinary-owner public projection precede query/page/JSON; ordinary OP has no inherited private-history access. Formal release/production deployment remain; integration history/public projection and explicit-read auditing passed |
| P1 | Close the small UI, documentation, validation and promotion changes | [#34](https://github.com/dynamicheart/lockbot/issues/34), PRs [#19](https://github.com/dynamicheart/lockbot/pull/19), [#35](https://github.com/dynamicheart/lockbot/pull/35), [#37](https://github.com/dynamicheart/lockbot/pull/37), [#38](https://github.com/dynamicheart/lockbot/pull/38). Keep the ordered device list; preserve custom promotion text; PR #19 is squash-merged as `8da2cf5` after six checks on reviewed `2343f4a`; the configurable localized prefix remains outside formal v2.15.2 (included in the integration test candidate). Legacy LogViewer scope PR #46 is squash-merged as `7a89b0c` after review and six green checks on final `da7b2ca`, but remains outside formal v2.15.2 (included in the integration test candidate). PR #38 and #37 are merged but unreleased. BotDetail parameter-only navigation refresh PR #47 is also merged as `0796763`, not yet formally released (included in the integration test candidate); verify current-head CI, merge and deployment separately |
| P1 | Finish Feishu alongside Infoflow | [PR #25](https://github.com/dynamicheart/lockbot/pull/25) and rendering gallery [#23](https://github.com/dynamicheart/lockbot/pull/23): release integration/regression, permissions, multi-group isolation, forms, migration and real message acceptance before formal rollout |
| P1 | Make client privacy handoffs verifiable | [#24](https://github.com/dynamicheart/lockbot/issues/24), [PR #43](https://github.com/dynamicheart/lockbot/pull/43): stdlib reference gate and offline example implemented, with seven additional real in-process HTTP serialization/API cases passing. Conditional metadata cleanup is implemented separately in [PR #48](https://github.com/dynamicheart/lockbot/pull/48), with 76 related checks including 15 cleanup cases passing; PR #43 is squash-merged as `0bf7d55` after six checks on final `ef79531`; PR #48 is merged as `edcd476`; formal release and actual external-client adoption remain; real integration cleanup returned 412 for an old revision and succeeded for the current replacement revision. Server privacy and version hints do not prove an external Agent obeys them |
| P2 | Scoped OP history and explainable usage analysis | [#21](https://github.com/dynamicheart/lockbot/issues/21), [PR #44](https://github.com/dynamicheart/lockbot/pull/44) phase 1 merged as `77b9d67` after review and six checks; fixed public projection precedes query/page and OP keys are rechecked before/after reads. Formal release/deployment remain pending; the integration candidate has real report acceptance, and v2.15.2 does not include it. Start with operation counts and observed outcomes; verified card-hours require complete lifecycle evidence across chat, Agent, OP, expiry and renewal |
| P2 | Close telemetry portability and operational acceptance | [#28](https://github.com/dynamicheart/lockbot/issues/28): formal XPU rollout is complete; broader NVIDIA deployment, actual driver-lock/workload impact measurements remain; a real 390px iframe CSS viewport passed for 16 cards without horizontal overflow, but mobile-engine/touch acceptance is separate |
| P2 | Isolate data-source trust across environments | [#33](https://github.com/dynamicheart/lockbot/issues/33), [PR #41](https://github.com/dynamicheart/lockbot/pull/41): per-source signer registry merged as `01be77b` after independent review and eight CI checks passed; migration/deployment and explicit client transport rules remain. The specific bastion-network/VPN experiment is deferred, not an active deployment task |

## Important current boundaries

### Follow-up work after 3.0.0

- [#57](https://github.com/dynamicheart/lockbot/issues/57): versioned messaging application credentials, provider-specific validation and an additive legacy-compatible migration. Keep the tested 3.0.0 credential layout for this release; do not combine messaging credentials with Agent, OP or metrics grants.
- [#54](https://github.com/dynamicheart/lockbot/issues/54): durable lease lifecycle events, stable identifiers and coverage-aware audit reporting.
- [#56](https://github.com/dynamicheart/lockbot/issues/56): explicitly delegated, scoped and revocable super-administrator access to detailed Agent notes. Ordinary OP credentials do not inherit this capability.
- Production Feishu application permissions and publication require a concrete review before submission. Test-enterprise applications must not be copied to a production enterprise.

### Agent task privacy

Released v2.15.1 hides free-form `purpose`, Agent names and credential/authorization details from group notifications, protected shared pages and other users' Agent reads. Only the optional server preset `task_category` is public; old clients may omit it and no free-form note is auto-classified.

The **current v2.15.2 release restricts private task fields to the same actor and super administrators**. Ordinary bot administration and OP maintenance do not grant other-user private reads. Actual formal-service ordinary OP and owner-management projection checks passed; no new test credentials were created. Reviewed role regressions cover own-actor/super-admin behavior; do not present them as every role freshly exercised against production. A later automation capability for private analysis must be explicitly delegated; `metrics:read` and an ordinary OP key do not grant it. OS/root file access is outside application RBAC.

### Telemetry

Lockbot's service does not collect hardware data, retain time series or proxy ordinary metric queries. The repository ships an independently deployed exporter, Prometheus and a query gateway. Browsers and Agents query that data source directly; the service may issue short-lived, bot/node-scoped metrics grants through an explicitly configured trust relationship. Data-source credentials, CORS, network reachability and task-private permissions are separate concerns.

Default hardware collection is staggered at five-minute intervals; page queries read cached samples and do not trigger SMI. Missing, failed or stale readings stay unknown, not zero. Low utilization never grants a lock or triggers release, renewal, preemption or process termination. Available memory and buffers/cache are distinct metrics; cache alone is not a driver-OOM diagnosis. The released automatic signer still uses one configured HMAC key; it is not multi-source trust isolation. Merged PR #41 (`01be77b`) adds an optional deployment-only registry with independent keys, strict audience matching, fail-closed selection and reload-on-issuance rotation/revocation semantics. Existing tokens still require gateway revocation or expiry; deployment has not changed.

### Feishu

One enterprise application routes explicitly configured resource groups to independent resource bots. Application connectivity and resource-bot running state must both be checked. Stop old local/test receivers before moving an application connection; a successful handshake alone does not prove resource operations or replies are ready.

PR #25 now adds super-admin-only persistent site proxy modes: follow the deployment setting (default), direct, or a credential-free HTTP(S) proxy. One authoritative policy covers Feishu HTTP/SDK discovery/WebSocket; only effective changes clear caches and stop old receivers before reconnecting. Public settings exclude proxy addresses. Fixed official HTTP probing explicitly leaves WebSocket untested. This site policy does not change global, Infoflow or metric-source proxies; per-application independent proxies remain outside this change. Real group tests include emoji, links and mentions; one successful message cannot replace the reusable rendering matrix or multi-group isolation tests.

## Released foundations

| Scope | Merged work |
| --- | --- |
| Agent personal binding, OP operations/Skill and queued broadcasts including optional mentions | [#1](https://github.com/dynamicheart/lockbot/pull/1); maintenance keys remain distinct from ordinary Agent keys |
| Legacy state files, Docker data path, additive upgrade and rollback guidance | [#2](https://github.com/dynamicheart/lockbot/pull/2), [upgrade design](docs/design/upgrade-rollback.md); a code rollback can restore older privacy behavior even when the database remains readable |
| Key-only bot discovery, service/instance identity, Skill installation and multi-bot connection records | [#4](https://github.com/dynamicheart/lockbot/pull/4), [#12](https://github.com/dynamicheart/lockbot/pull/12), [#16](https://github.com/dynamicheart/lockbot/pull/16); update discovery completed in [#29](https://github.com/dynamicheart/lockbot/issues/29), not a guarantee of external-client reload |
| Protected per-bot queue pages, daily lazy link generation, short links, visible queues and overview selection | [#6](https://github.com/dynamicheart/lockbot/pull/6), [#10](https://github.com/dynamicheart/lockbot/pull/10), [#11](https://github.com/dynamicheart/lockbot/pull/11) |
| HTTP traffic and private rotating Agent JSONL files | [#8](https://github.com/dynamicheart/lockbot/pull/8); the richer administration UI in PR #22 is merged but not formally released (included in the integration test candidate) |
| QUEUE_DEVICE, preview/model display and optional continuous-device display merging | [#9](https://github.com/dynamicheart/lockbot/pull/9), [#13](https://github.com/dynamicheart/lockbot/pull/13); merging is not enabled by default |
| Renewal prohibition, per-person resource caps and form spacing | [#15](https://github.com/dynamicheart/lockbot/pull/15), [#17](https://github.com/dynamicheart/lockbot/pull/17); existing bots default to allowing renewal and unlimited resource counts |
| Standalone removal and malformed-command feedback | [#14](https://github.com/dynamicheart/lockbot/pull/14), [#31](https://github.com/dynamicheart/lockbot/pull/31); malformed nonempty input is rejected rather than silently interpreted as query |
| Public task-category privacy, real telemetry and reproducible offline bundles | [#26](https://github.com/dynamicheart/lockbot/pull/26), [#20](https://github.com/dynamicheart/lockbot/pull/20), [#30](https://github.com/dynamicheart/lockbot/pull/30), [#32](https://github.com/dynamicheart/lockbot/pull/32), released through v2.15.1 |
| Private task-role separation and reliable trailing notifications | [#40](https://github.com/dynamicheart/lockbot/pull/40), [#42](https://github.com/dynamicheart/lockbot/pull/42), focused v2.15.2 release [#45](https://github.com/dynamicheart/lockbot/pull/45); release/build/formal upgrade and scoped real acceptance completed |

## v2.15.2 acceptance record

The authorized test-group burst completed three mutations in 0.358 seconds, then recorded two platform-accepted notifications and two queued events over 12 seconds without another triggering request. Lock/configuration cleanup was verified privately. This is not recipient read confirmation or a promise of durable delivery; already-lost historical notifications are not reconstructed.

The published image archive SHA-256 is `f29e21e655bdcf9ecdc0d475cae51ded0c4c5932b822df90286693e945032596` (artifact ID `12884505359`). The formal service serves Agent Skill `7197d0e6d8316d46` and OP Skill `9e6c6949e73bc662`. Runtime credentials, addresses, deployment journal paths and raw evidence stay private.

## Product and release boundaries

- Support Infoflow and Feishu. Slack, DingTalk, WeChat, serverless and distributed-state deployments are not scheduled.
- Ignore Feishu direct messages; do not add a private-chat help reply or require private-message permissions.
- Fix platform identity when creating a bot. Migration copies configuration into a new form and excludes credentials, group/application associations, authorizations, live state, history and platform-user exception lists.
- Do not build a shared resource pool across platforms or attach one resource bot to both platforms. Do not run independent lock systems over the same physical resources during migration.
- Agent contact registration and communication were an early discussion idea, not an implemented or explicitly committed delivery. Do not infer them as an extension of purpose notes; current resource/usage queries remain the collaboration surface while that decision is open.
- The isolated-environment VPN/IT request is deferred by the user. Do not restart it as an implied dependency of telemetry.
- Latest explicit release decision: develop and merge all current PRs on `release/2.x` through 3.0.0 testing/acceptance. After acceptance, preserve historical `main`, align `main` to the exact final `release/2.x` commit, then make `main` the default. Do not rename the release branch; the main agent coordinates execution. The 3.0.0 target is approved, but alignment/default changes and release are not recorded as completed.
- Require checks on the current PR head and prefer squash merges. Branch protection was checked on this date: force pushes/deletion disabled, protection enforced for administrators, strict current-base checks and PR merging required, and all six CI contexts required. Recheck before release rather than relying on this snapshot.
- Track merge, version/tag, image/artifact build and checksum, deployment, configuration/key/state preservation and real acceptance explicitly. Do not turn a test-service upgrade or a client screenshot into a formal release claim.


## Latest user feedback

- Audit action translation PR #50 is merged as `88a9a11`; display labels only, no permission changes, formal release pending.
- Current test-service group-name read and explicit refresh returned HTTP 200 with a name. PR #25 now marks retained names as cached after transient failure, distinguishes permission vs temporary failures and offers retry. No missing-permission claim is inferred from the earlier warning.
- Earlier PR #25 integration candidate `592e9b0` passed its recorded backend/frontend suites and deployment checks with configuration preserved. The current integration deployment is `810ae4b`; committed UI follow-up `c771736` and pending application-management work are distinguished in the delivery snapshot. Site proxy, group-name HTTP lookup/refresh and a real authorized-group query/reply are verified. Formal production remains v2.15.2 without Feishu. Default/explicit detailed-note reads and prior real two-group acceptance passed; repeat acceptance when later changes affect routing/rendering. Explicit opt-in for Agent detailed notes (#52) and layered operation trends (#51) are merged and included in the integration candidate, outside formal production.


### Approved 3.0.0 branch migration sequence

The latest user decision supersedes the earlier deferred version choice. Before changing the default line, preserve the existing historical `main` as `archive/main-before-v3-20261029`. It has 24 unique historical commits while the maintained release line has 75 unique commits at the comparison snapshot; do not force-push over that history or blindly merge the abandoned generic-platform/credentials-JSON implementation back into the product.

Continue development, PR targeting and merges on `release/2.x` until the 3.0.0 candidate passes required testing/acceptance. Then align `main` to the exact final commit of `release/2.x`, copy the six mandatory CI checks/protection and update CI/telemetry/docs/default-branch references as coordinated by the main agent. This is commit alignment, not renaming `release/2.x` to `main`. Current PRs keep their release target during development; stop maintaining the release line only after the accepted transition. The main agent coordinates the exact execution order. No branch deletion, force push, default change or release is recorded as completed by this document update.

See [upgrade and database compatibility](docs/design/upgrade-rollback.md#飞书-300-的数据库与部署边界) for the additive schema and rollback boundaries.

## Latest integrated acceptance and deployment guidance

- PR #51 operation-source trends and PR #52 explicit **Agent detailed notes** access are merged. Default administration reads remain public; the super administrator must explicitly opt in, which produces an audit event. The latest committed bot-detail UI (`932c692`) places this control only in Agent operation history; ordinary logs, monitoring, access statistics and authorization records remain public-only, and leaving operation history clears the opt-in. Actual integration checks observed zero events from three default reads and one from one explicit read. The UI term is “Agent 详细任务备注”; technical permission/field identifiers remain unchanged.
- Real integration lifecycle reports return complete intervals within the current process and accurately return unknown coverage across restart gaps. Conditional declaration cleanup rejects stale If-Match revisions with 412 and accepts the current revision. This does not prove external Agents adopt the handoff helper or measure actual hardware/work hours.
- Hosting guidance PR #55 is merged (`445bea7`). Deployment/onboarding guidance and the read-only preflight helper in [PR #53](https://github.com/dynamicheart/lockbot/pull/53) are squash-merged after six successful checks on final `8976e94`; the oversized-port regression now returns structured failure without a traceback. Twelve focused CLI checks and Ruff passed; Compose/container execution was not performed locally because Docker CLI was unavailable. These instructions do not authorize deployment, permission expansion or real group messages.
- Private proof and sanitized PR #25 acceptance summaries are tracked in [the integration record](https://github.com/dynamicheart/lockbot/pull/25#issuecomment-6500258587). Hardware workload/driver-lock measurements and external-client adoption remain open.

### Latest administration UI and pending application management

Committed PR #25 administration UI work centers bounded site settings, simplifies broadcast preview/results, separates Agent access statistics / operation history / authorization records, limits the detailed-note switch to Agent operation history, keeps other bot-detail views public-only, and moves long monitoring explanations into tooltips. Feishu application names and receiver state are displayed explicitly, and resource-type labels preserve uppercase `QUEUE_DEVICE`. Integration screenshot/API acceptance is recorded privately. The 169-test frontend result is not proof of formal rollout or a hardware-performance measurement.

Commit `27e3cb8` adds platform filters and further simplifies the broadcast dialog; these committed changes are newer than deployed `810ae4b`. Commit `c771736` defaults the bot list and administrative bot monitor to running bots; stopped bots remain selectable through the status filter. Application management is still **pending tests and commit**: do not claim application rename/edit, credential rotation or guarded deletion is released, deployed or accepted before that work is verified. Corporate permission/approval review and post-3.0.0 issues #54/#56/#57 remain unchanged.

</details>

### Optional Feishu query panels (#62)

An independent implementation adds default-off per-bot in-place updates for full group queries only. Original text/post rendering, Infoflow delivery, explicit operation outcomes and critical reminders remain unchanged. Verified own-app/group message references survive restart; scoped serialization, identity generations, conservative rollover and bounded fallback protect routing and concurrency. Missing/revoked editing permission requires no expanded baseline scopes. Automated API/transport tests are recorded in the PR; live authorized-group acceptance and rollout remain pending root review. See [configuration and limitations](docs/usage/feishu.md#可选原地更新查询面板).

</details>
