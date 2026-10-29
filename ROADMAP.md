# Roadmap

Updated 2026-10-29. Track the current checklist in [Roadmap issue #27](https://github.com/dynamicheart/lockbot/issues/27). Implementation, merge, release and production deployment are separate states; an open PR does not mean a feature is available in production.

## Current priorities

| Priority | Work | Status |
| --- | --- | --- |
| P0 | Private Agent notes and names; preset public task categories; first-contact Skill installation and scoped connection records | [PR #26](https://github.com/dynamicheart/lockbot/pull/26), not merged |
| P1 | Configurable localized Agent promotion text, preserving links and message spacing | [PR #19](https://github.com/dynamicheart/lockbot/pull/19), not merged |
| P1 | Bot- and credential-scoped monitoring, operation sources, task history and OP access | [PR #22](https://github.com/dynamicheart/lockbot/pull/22), not merged; analysis follow-up in [issue #21](https://github.com/dynamicheart/lockbot/issues/21) |
| P1 | Feishu alongside Infoflow, with one enterprise app routing to independent resource groups | [PR #25](https://github.com/dynamicheart/lockbot/pull/25), real test-group acceptance in progress |
| P1 | Reproducible Feishu emoji, hyperlink and mention rendering checks | [PR #23](https://github.com/dynamicheart/lockbot/pull/23), not merged |
| P2 | Optional browser-side utilization data sources, configured from bot details | [PR #20](https://github.com/dynamicheart/lockbot/pull/20), real data-source acceptance pending |

Feishu closure includes group association validation, cold-cache display names, platform-specific forms, configuration copying, per-application HTTP/WebSocket proxy support, permissions documentation and Infoflow regression checks. Some fixes are implemented but still need acceptance; see issue #27 for the individual items.

Utilization remains independent of resource scheduling. Administrators configure a source and verify queries and device mappings; browsers query it directly. Missing, failed or stale metrics must not become zero values or interfere with lock and queue operations. Lockbot does not collect, proxy or retain metrics. Prometheus is the first adapter; a real source is still needed for end-to-end acceptance.

## Merged foundations

- Agent resource APIs, personal enrollment, OP operations and queued group broadcasts: [#1](https://github.com/dynamicheart/lockbot/pull/1).
- Legacy state-file and Docker data-directory compatibility: [#2](https://github.com/dynamicheart/lockbot/pull/2).
- Service identity, bot discovery from credentials, Skill revisions and credential isolation: [#4](https://github.com/dynamicheart/lockbot/pull/4).
- Protected per-bot resource/queue pages, short links and resource overviews: [#6](https://github.com/dynamicheart/lockbot/pull/6), [#10](https://github.com/dynamicheart/lockbot/pull/10), [#11](https://github.com/dynamicheart/lockbot/pull/11).
- HTTP traffic monitoring and file-based Agent operation history: [#8](https://github.com/dynamicheart/lockbot/pull/8).
- Per-card FIFO queues and optional device-range display merging: [#9](https://github.com/dynamicheart/lockbot/pull/9), [#13](https://github.com/dynamicheart/lockbot/pull/13).
- Polling, lease-expiry, renewal and multi-bot credential guidance: [#12](https://github.com/dynamicheart/lockbot/pull/12), [#16](https://github.com/dynamicheart/lockbot/pull/16). The public-purpose guidance in #16 is superseded by the privacy policy proposed in #26.
- Standalone mode removal: [#14](https://github.com/dynamicheart/lockbot/pull/14).
- Renewal restrictions, per-user resource limits and form fixes: [#15](https://github.com/dynamicheart/lockbot/pull/15), [#17](https://github.com/dynamicheart/lockbot/pull/17).

## Product boundaries

- Support Infoflow and Feishu. Slack, DingTalk, WeChat, serverless and distributed-state deployments are not scheduled.
- Feishu direct messages are unsupported and ignored, without an automatic help reply.
- Set a bot's platform at creation. Migration copies configuration into a new form, excluding credentials, group/app associations, authorizations, live state and platform-user exceptions.
- No shared resource pool across platforms and no single resource bot attached to both platforms. Do not create independent locks over the same physical resources during migration.
- Do not release, preempt, renew or stop workloads automatically based on utilization.
- Private task notes remain available to their owner, bot administrators and authorized OP credentials; group reports use suitable categories and aggregate information.

## Release policy

Maintain `release/2.x` while the integration is completed. A 3.x release and a return to `main` need a separate compatibility and release decision. Require current-head CI and prefer squash merges. Track tags, image builds and production upgrades explicitly, including upgrade checks and rollback limitations.

The former calendar-based multi-platform/cloud wishlist has been replaced by this confirmed scope. This roadmap does not assign delivery dates to unverified integrations.
