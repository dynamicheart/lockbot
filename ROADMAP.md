# Roadmap

Updated 2026-10-29. [Roadmap issue #27](https://github.com/dynamicheart/lockbot/issues/27) tracks execution and [the requirement coverage matrix](docs/design/requirement-coverage.md) maps the discussion to evidence and remaining acceptance. Implementation, merge, release, deployment and acceptance are different states. An open PR or a successful local test is not a production rollout.

## Delivery snapshot

- The formal deployment is **v2.15.1**. Public Agent task-category privacy, API/Skill update signals, protected queue pages, real per-card telemetry and reproducible telemetry bundles are released. Actual Agent API and browser checks cover 16 nodes / 64 cards; anonymous source access and out-of-scope queries were denied.
- The integration test deployment has an older **v2.14.0 code baseline with selected Feishu proxy/handshake patches**. Real WebSocket handshake, connector recovery and a fresh human group lock/reply passed after removing a duplicate local receiver and starting the routed resource bot. This does not mean the entire Feishu PR or the latest formal privacy policy is deployed there.
- [PR #19](https://github.com/dynamicheart/lockbot/pull/19), [#22](https://github.com/dynamicheart/lockbot/pull/22), [#23](https://github.com/dynamicheart/lockbot/pull/23), [#25](https://github.com/dynamicheart/lockbot/pull/25), [#35](https://github.com/dynamicheart/lockbot/pull/35), [#37](https://github.com/dynamicheart/lockbot/pull/37) and [#38](https://github.com/dynamicheart/lockbot/pull/38) remain separate, unmerged deliveries at this snapshot. CI must be checked on each current head.
- The stricter private-task permission policy in [#36](https://github.com/dynamicheart/lockbot/issues/36) and reliable trailing notifications in [#39](https://github.com/dynamicheart/lockbot/issues/39) are being implemented. Neither is described as already released.

## Priorities and dependency order

| Priority | Work | Delivery boundary and next gate |
| --- | --- | --- |
| P0 | Stop silently losing Agent resource notifications | [#39](https://github.com/dynamicheart/lockbot/issues/39): bounded per-bot trailing batches, independent of another request; preserve routing, privacy and uncertain-send semantics; deterministic tests, test-group acceptance, then a reviewed patch rollout |
| P0 | Separate private task access from bot administration and OP maintenance | [#36](https://github.com/dynamicheart/lockbot/issues/36): retain own-user access; default other-user access to super administrators; server-side coverage across current state, history, downloads and OP routes; update Skills and metadata before rollout |
| P1 | Finish private history and scoped service monitoring | [PR #22](https://github.com/dynamicheart/lockbot/pull/22), gated by #36. Ordinary CMD logs, Agent request counters and private task history must remain distinguishable; global monitoring remains super-admin scoped |
| P1 | Close the small UI, documentation, validation and promotion changes | [#34](https://github.com/dynamicheart/lockbot/issues/34), PRs [#19](https://github.com/dynamicheart/lockbot/pull/19), [#35](https://github.com/dynamicheart/lockbot/pull/35), [#37](https://github.com/dynamicheart/lockbot/pull/37), [#38](https://github.com/dynamicheart/lockbot/pull/38). Keep the ordered device list; preserve custom promotion text; verify current-head CI, merge and deployment separately |
| P1 | Finish Feishu alongside Infoflow | [PR #25](https://github.com/dynamicheart/lockbot/pull/25) and rendering gallery [#23](https://github.com/dynamicheart/lockbot/pull/23): release integration/regression, permissions, multi-group isolation, forms, migration and real message acceptance before formal rollout |
| P1 | Make client privacy handoffs verifiable | [#24](https://github.com/dynamicheart/lockbot/issues/24): client-side policy propagation, final-payload validation, command-only handoff and feedback routing. Server privacy and version hints do not prove an external Agent obeys them |
| P2 | Scoped OP history and explainable usage analysis | [#21](https://github.com/dynamicheart/lockbot/issues/21), after #36 / PR #22. Start with operation counts and observed outcomes; full card-hours require complete lifecycle evidence across chat, Agent, OP, expiry and renewal |
| P2 | Close telemetry portability and operational acceptance | [#28](https://github.com/dynamicheart/lockbot/issues/28): formal XPU rollout is complete; broader NVIDIA deployment, actual driver-lock/workload impact measurements and narrow-browser checks remain distinct acceptance items |
| P2 | Isolate data-source trust across environments | [#33](https://github.com/dynamicheart/lockbot/issues/33): per-source signer isolation or asymmetric verification and explicit client transport rules. The specific bastion-network/VPN experiment is deferred, not an active deployment task |

## Important current boundaries

### Agent task privacy

Released v2.15.1 hides free-form `purpose`, Agent names and credential/authorization details from group notifications, protected shared pages and other users' Agent reads. Only the optional server preset `task_category` is public; old clients may omit it and no free-form note is auto-classified.

The **current release still permits bot administrators and ordinary bot-scoped OP credentials to read private task information**. The new requirement in #36 changes that default to the actor and super administrators. Do not present this desired policy as current enforcement. A later automation capability for private analysis must be explicitly delegated; `metrics:read` and an ordinary OP key do not grant it. OS/root file access is outside application RBAC.

### Telemetry

Lockbot's service does not collect hardware data, retain time series or proxy ordinary metric queries. The repository ships an independently deployed exporter, Prometheus and a query gateway. Browsers and Agents query that data source directly; the service may issue short-lived, bot/node-scoped metrics grants through an explicitly configured trust relationship. Data-source credentials, CORS, network reachability and task-private permissions are separate concerns.

Default hardware collection is staggered at five-minute intervals; page queries read cached samples and do not trigger SMI. Missing, failed or stale readings stay unknown, not zero. Low utilization never grants a lock or triggers release, renewal, preemption or process termination. Available memory and buffers/cache are distinct metrics; cache alone is not a driver-OOM diagnosis. The current automatic signer uses one configured HMAC key; it is not multi-source trust isolation.

### Feishu

One enterprise application routes explicitly configured resource groups to independent resource bots. Application connectivity and resource-bot running state must both be checked. Stop old local/test receivers before moving an application connection; a successful handshake alone does not prove resource operations or replies are ready.

The current proxy implementation in PR #25 is **deployment-scoped for Feishu applications**, covering HTTP API, SDK handshake and WebSocket without changing global proxies or Infoflow transport. Per-application proxy UI remains separate work. Real group tests include emoji, links and mentions; one successful message cannot replace the reusable rendering matrix or multi-group isolation tests.

## Released foundations

| Scope | Merged work |
| --- | --- |
| Agent personal binding, OP operations/Skill and queued broadcasts including optional mentions | [#1](https://github.com/dynamicheart/lockbot/pull/1); maintenance keys remain distinct from ordinary Agent keys |
| Legacy state files, Docker data path, additive upgrade and rollback guidance | [#2](https://github.com/dynamicheart/lockbot/pull/2), [upgrade design](docs/design/upgrade-rollback.md); a code rollback can restore older privacy behavior even when the database remains readable |
| Key-only bot discovery, service/instance identity, Skill installation and multi-bot connection records | [#4](https://github.com/dynamicheart/lockbot/pull/4), [#12](https://github.com/dynamicheart/lockbot/pull/12), [#16](https://github.com/dynamicheart/lockbot/pull/16); update discovery completed in [#29](https://github.com/dynamicheart/lockbot/issues/29), not a guarantee of external-client reload |
| Protected per-bot queue pages, daily lazy link generation, short links, visible queues and overview selection | [#6](https://github.com/dynamicheart/lockbot/pull/6), [#10](https://github.com/dynamicheart/lockbot/pull/10), [#11](https://github.com/dynamicheart/lockbot/pull/11) |
| HTTP traffic and private rotating Agent JSONL files | [#8](https://github.com/dynamicheart/lockbot/pull/8); the richer administration UI is still PR #22 |
| QUEUE_DEVICE, preview/model display and optional continuous-device display merging | [#9](https://github.com/dynamicheart/lockbot/pull/9), [#13](https://github.com/dynamicheart/lockbot/pull/13); merging is not enabled by default |
| Renewal prohibition, per-person resource caps and form spacing | [#15](https://github.com/dynamicheart/lockbot/pull/15), [#17](https://github.com/dynamicheart/lockbot/pull/17); existing bots default to allowing renewal and unlimited resource counts |
| Standalone removal and malformed-command feedback | [#14](https://github.com/dynamicheart/lockbot/pull/14), [#31](https://github.com/dynamicheart/lockbot/pull/31); malformed nonempty input is rejected rather than silently interpreted as query |
| Public task-category privacy, real telemetry and reproducible offline bundles | [#26](https://github.com/dynamicheart/lockbot/pull/26), [#20](https://github.com/dynamicheart/lockbot/pull/20), [#30](https://github.com/dynamicheart/lockbot/pull/30), [#32](https://github.com/dynamicheart/lockbot/pull/32), released through v2.15.1 |

## Product and release boundaries

- Support Infoflow and Feishu. Slack, DingTalk, WeChat, serverless and distributed-state deployments are not scheduled.
- Ignore Feishu direct messages; do not add a private-chat help reply or require private-message permissions.
- Fix platform identity when creating a bot. Migration copies configuration into a new form and excludes credentials, group/application associations, authorizations, live state, history and platform-user exception lists.
- Do not build a shared resource pool across platforms or attach one resource bot to both platforms. Do not run independent lock systems over the same physical resources during migration.
- Agent contact registration and communication were an early discussion idea, not an implemented or explicitly committed delivery. Do not infer them as an extension of purpose notes; current resource/usage queries remain the collaboration surface while that decision is open.
- The isolated-environment VPN/IT request is deferred by the user. Do not restart it as an implied dependency of telemetry.
- Maintain `release/2.x`. A 3.x release or return to `main` requires a separate compatibility/release decision; Feishu work alone does not automatically change the branch or version line.
- Require checks on the current PR head and prefer squash merges. Branch protection was checked on this date: force pushes/deletion disabled, protection enforced for administrators, strict current-base checks and PR merging required, and all six CI contexts required. Recheck before release rather than relying on this snapshot.
- Track merge, version/tag, image/artifact build and checksum, deployment, configuration/key/state preservation and real acceptance explicitly. Do not turn a test-service upgrade or a client screenshot into a formal release claim.
