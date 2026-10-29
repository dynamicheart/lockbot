# Roadmap

Updated 2026-10-29. [Roadmap issue #27](https://github.com/dynamicheart/lockbot/issues/27) tracks execution and [the requirement coverage matrix](docs/design/requirement-coverage.md) maps the discussion to evidence and remaining acceptance. Implementation, merge, release, deployment and acceptance are different states. An open PR or a successful local test is not a production rollout.

## Delivery snapshot

| State | Evidence and next gate |
| --- | --- |
| Formal deployment | **[v2.15.2](https://github.com/dynamicheart/lockbot/releases/tag/v2.15.2)**, release commit `f4badac`. The two P0 fixes are released: private-task role separation (#40) and trailing notification delivery (#42). Existing keys/configuration/locks/queues were preserved; actual formal OP/owner projection, Skill/version and direct-source 16-node/64-card checks passed. |
| Merged, not released or deployed | Promotion #19, private history/scoped monitoring #22, authorization documentation #35, safe category validation #37, resource layout #38, per-source signers #41, client handoff #43, public OP history/report #44, log scope #46, detail route scope #47 conditional declaration cleanup #48, verified lifecycle accounting #49 and audit action translation #50 (`88a9a11`). Each passed its final required checks before squash merge; PR/Issue records retain reviewed heads and test evidence. These are outside formal v2.15.2. |
| Lifecycle accounting candidate | [PR #49](https://github.com/dynamicheart/lockbot/pull/49): committed allocation evidence and verified interval reporting. Strict checkpoint validation fixes a reproduced false-coverage bug. Squash-merged as `ef00d76` after independent review, 114 frozen-candidate targeted checks and six final-head CI checks on `a6fe893`. Release and real report acceptance remain required. This measures grants, never hardware utilization or actual work. |
| Feishu candidate | [PR #25](https://github.com/dynamicheart/lockbot/pull/25) remains a draft awaiting final release integration and real acceptance, alongside explicit rendering gallery #23. The test service now runs candidate `5c7f3f4`, integrated with release base `ef00d76`; all 129 backend package files and the built UI assets were checked against the candidate. Existing bot/binding/application/route configuration was preserved, and one dedicated-proxy receiver was confirmed. This is a test deployment, not formal release or completed real-group privacy acceptance. Formal v2.15.2 does not include Feishu. |
| Real acceptance preparation | A second temporary Feishu group was explicitly authorized, created through the logged-in user UI, and its original application/bot and verified owner were confirmed through API; counts show one user and one bot. Detailed member-list permission is unavailable and was not expanded. No current-candidate two-group success is claimed yet. Finish isolation, messages, cleanup and browser acceptance before rollout. |
| Remaining work | Real external-client adoption/task cleanup (#24), complete lifecycle/report acceptance (#21), telemetry operational/performance acceptance (#28), and source-trust rollout boundaries (#33). The requirement coverage matrix distinguishes delivered behavior, missing evidence, limitations and user-deferred network work. |

Merge, tag/release, image/artifact build, deployment and real acceptance remain separate gates. Issues #36/#39 are closed for their delivered P0 scope; subsequent endpoints and candidates need their own checks.

## Priorities and dependency order

| Priority | Work | Delivery boundary and next gate |
| --- | --- | --- |
| P0 | Stop silently losing Agent resource notifications | [#39](https://github.com/dynamicheart/lockbot/issues/39), [PR #42](https://github.com/dynamicheart/lockbot/pull/42): released/deployed in v2.15.2. Real authorized test-group acceptance observed the trailing resource notice without a fourth request; pending batches remain in-memory best effort and uncertain sends are not retried |
| P0 | Separate private task access from bot administration and OP maintenance | [#36](https://github.com/dynamicheart/lockbot/issues/36), [PR #40](https://github.com/dynamicheart/lockbot/pull/40): squash-merged after fresh review and six green checks; retain own-user access and default other-user access to super administrators. Existing server projections, bundled Skills and metadata are released; actual formal OP/owner projection checks passed. Future history/search/export surfaces must reuse and separately validate this policy |
| P1 | Finish private history and scoped service monitoring | [PR #22](https://github.com/dynamicheart/lockbot/pull/22) merged as `d3c86e1` after six checks; 62 focused backend and 97 frontend tests passed. Current authority and ordinary-owner public projection precede query/page/JSON; ordinary OP has no inherited private-history access. Release and actual deployment acceptance remain |
| P1 | Close the small UI, documentation, validation and promotion changes | [#34](https://github.com/dynamicheart/lockbot/issues/34), PRs [#19](https://github.com/dynamicheart/lockbot/pull/19), [#35](https://github.com/dynamicheart/lockbot/pull/35), [#37](https://github.com/dynamicheart/lockbot/pull/37), [#38](https://github.com/dynamicheart/lockbot/pull/38). Keep the ordered device list; preserve custom promotion text; PR #19 is squash-merged as `8da2cf5` after six checks on reviewed `2343f4a`; the configurable localized prefix remains unreleased and undeployed. Legacy LogViewer scope PR #46 is squash-merged as `7a89b0c` after review and six green checks on final `da7b2ca`, but remains unreleased/undeployed. PR #38 and #37 are merged but unreleased. BotDetail parameter-only navigation refresh PR #47 is also merged as `0796763`, not yet released or deployed; verify current-head CI, merge and deployment separately |
| P1 | Finish Feishu alongside Infoflow | [PR #25](https://github.com/dynamicheart/lockbot/pull/25) and rendering gallery [#23](https://github.com/dynamicheart/lockbot/pull/23): release integration/regression, permissions, multi-group isolation, forms, migration and real message acceptance before formal rollout |
| P1 | Make client privacy handoffs verifiable | [#24](https://github.com/dynamicheart/lockbot/issues/24), [PR #43](https://github.com/dynamicheart/lockbot/pull/43): stdlib reference gate and offline example implemented, with seven additional real in-process HTTP serialization/API cases passing. Conditional metadata cleanup is implemented separately in [PR #48](https://github.com/dynamicheart/lockbot/pull/48), with 76 related checks including 15 cleanup cases passing; PR #43 is squash-merged as `0bf7d55` after six checks on final `ef79531`; PR #48 is merged as `edcd476`; release, real task-cleanup acceptance and actual external-client adoption remain. Server privacy and version hints do not prove an external Agent obeys them |
| P2 | Scoped OP history and explainable usage analysis | [#21](https://github.com/dynamicheart/lockbot/issues/21), [PR #44](https://github.com/dynamicheart/lockbot/pull/44) phase 1 merged as `77b9d67` after review and six checks; fixed public projection precedes query/page and OP keys are rechecked before/after reads. Release/deployment remain pending; v2.15.2 does not include it. Start with operation counts and observed outcomes; full card-hours require complete lifecycle evidence across chat, Agent, OP, expiry and renewal |
| P2 | Close telemetry portability and operational acceptance | [#28](https://github.com/dynamicheart/lockbot/issues/28): formal XPU rollout is complete; broader NVIDIA deployment, actual driver-lock/workload impact measurements remain; a real 390px iframe CSS viewport passed for 16 cards without horizontal overflow, but mobile-engine/touch acceptance is separate |
| P2 | Isolate data-source trust across environments | [#33](https://github.com/dynamicheart/lockbot/issues/33), [PR #41](https://github.com/dynamicheart/lockbot/pull/41): per-source signer registry merged as `01be77b` after independent review and eight CI checks passed; migration/deployment and explicit client transport rules remain. The specific bastion-network/VPN experiment is deferred, not an active deployment task |

## Important current boundaries

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
| HTTP traffic and private rotating Agent JSONL files | [#8](https://github.com/dynamicheart/lockbot/pull/8); the richer administration UI in PR #22 is merged but not released/deployed |
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
- Latest explicit release decision: ship the Feishu integration as **3.0.0**, return the default development line to `main`, and stop maintaining `release/2.x`. The main agent is checking branch differences/protection and coordinating PR retargeting before executing migration; this decision does not imply the branch migration or release is already complete.
- Require checks on the current PR head and prefer squash merges. Branch protection was checked on this date: force pushes/deletion disabled, protection enforced for administrators, strict current-base checks and PR merging required, and all six CI contexts required. Recheck before release rather than relying on this snapshot.
- Track merge, version/tag, image/artifact build and checksum, deployment, configuration/key/state preservation and real acceptance explicitly. Do not turn a test-service upgrade or a client screenshot into a formal release claim.


## Latest user feedback

- Audit action translation PR #50 is merged as `88a9a11`; display labels only, no permission changes, formal release pending.
- Current test-service group-name read and explicit refresh returned HTTP 200 with a name. PR #25 now marks retained names as cached after transient failure, distinguishes permission vs temporary failures and offers retry. No missing-permission claim is inferred from the earlier warning.
- New PR #25 head `20dff0e` integrates `88a9a11` and the site proxy/cache feedback. Local frontend 136 checks/lint/build, 212 cross-layer focused tests and 10 overlapping final proxy/connector tests passed; current-head CI is running. It remains draft and is not yet deployed. The deployed test source is still `5c7f3f4`, and formal production remains v2.15.2 without Feishu. Real proxy-save/restart/handshake and complete current-candidate group acceptance remain pending.


### Approved 3.0.0 branch migration sequence

The latest user decision supersedes the earlier deferred version choice. Before changing the default line, preserve the existing historical `main` as `archive/main-before-v3-20261029`. It has 24 unique historical commits while the maintained release line has 75 unique commits at the comparison snapshot; do not force-push over that history or blindly merge the abandoned generic-platform/credentials-JSON implementation back into the product.

After the integrated candidate passes required acceptance, migrate the maintained release history to `main`, copy the six mandatory CI checks and protection policy, retarget open PRs, update CI/telemetry/docs branch references, and make `main` the default with 3.0.0. Stop maintaining `release/2.x` afterward. The main agent coordinates the exact execution order. No branch deletion, force push, default change or release is recorded as completed by this document update.

See [upgrade and database compatibility](docs/design/upgrade-rollback.md#飞书-300-的数据库与部署边界) for the additive schema and rollback boundaries.
