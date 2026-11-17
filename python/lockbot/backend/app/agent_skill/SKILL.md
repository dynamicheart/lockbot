---
name: lockbot-agent
description: >
  Connect to a LockBot resource group, find saved connections, check shared compute,
  lock or book nodes and GPU cards, release holdings, or open the personal workbench.
  Uses the LockBot Resource API (lockbot.resource-api) with scoped lbr_ credentials.
  The user confirms new enrollment in their group chat.
---

# LockBot

<!-- LOCKBOT_DEPLOYMENT_API_ADDRESSES -->

Run bundled `scripts/lockbot_cli.py` by absolute path, Python 3.10+. Use `connections`, then its `--connection` with `resources` or `command --operation <verb>`. Keep verified service/instance/bot/actor scopes separate. No Keys/grants in replies, arguments, Skill/shared memory. Default to private user storage, respecting `LOCKBOT_HOME`; project scope only on request.

## Everyday requests

- Find/availability is READ ONLY: read eligible groups, report choices, stop. A later “lock 一天” authorizes the uniquely selected machine; clarify real ambiguity only.
- “我能申请什么”: `recover` once from a verified seed when supported; reuse verified groups and read their own connections. “我lock/book了哪些”: check ALL saved authorized groups; match actor exactly and separate holdings/cards/bookings. Bookings are not ownership; holder time is not booking expiry/guaranteed start. Label estimates/claim windows.
- “到时间提醒”: read own resources FIRST. `next_event_kind: own_early_warning` is an existing group warning, not a new timer or proof of delivery. Expiry alone doesn't prove a warning. This API cannot create personal timers.
- Reply briefly using `remaining_time`; exact deadlines use `--exact-lease-times`. Missing time is UNKNOWN, never unlimited. Hide Key/path mechanics; group names only for ambiguity.

## Select and act

`resources` includes verified `context`/rules: avoid redundant `discover/context` and immediate verification rereads. Recheck later ownership/expiry; stop workload use if unverifiable.

1. Resolve exact IDs, aliases or authorized IPv4 suffixes across eligible groups. Names do not prove hardware. Require `snapshot_fresh_for_write`; verify model, quantity, mode, duration and same-node constraints. Stale/ambiguous/unverified means NO command. Pass requested `--model`, `--device-count`, `--minimum-model-count` and named `--group`.
2. Node-only `lock/slock/book`: whole-node groups use `--whole-node`; card groups enumerate ALL observed `--device`s in ONE command. No silent subset: report conflicts/shortfalls. Never turn card requests into whole nodes/change groups. Different card sets need separate node commands; stop on uncertainty.
3. Verify support; preserve chat operation/mode. Lock idle matches with empty queues; book only with queue authorization. New lock/slock/book: omit `--duration` if unspecified for the live default; ask if unavailable. `claim`: idle `claimable_by_you`, ORIGINAL booked duration, omit duration. `release`: exact holding/booking, no duration/rebooking.
4. A NEW explicit lock/slock on a fresh verified own holding in the SAME mode maps to CLI `renew`: ADD requested time or verified live group default (else ask), only if relock allowed. Never renew from polling/time passage/uncertain writes. Mixed/unknown modes stop.
5. ONE POST maximum; require `verified` + `verified_snapshot`. Fix proven zero-HTTP argument errors locally. Sent/uncertain writes: read/report, NO replay/alternate target/HTTP bypass. Honor `Retry-After`. Updates/incompatibility block writes until matching instructions load; no rebind.

## Install or connect

Bare link: inspect only. Explicit install: verify manifest/complete ZIP SHA-256, install/reload; install-only stops. Reuse a connection/verified installed `deployment_origin`, no backup/known-URL demand. With a privately supplied seed Key and known origin but no connection, `init --key-file` then `recover`; no new binding. Without access, `key-challenge --origin <approved-origin> --authorized` returns `user_instruction[_zh]`: quote it verbatim for your OWN chat account; it may restore identity. After confirmation, resume SAME pending enrollment via `enrollment-status`/`finish-enrollment`; fulfill normal follow-ups. Reset: explain old-Key invalidation/other-Agent impact and obtain acknowledgement BEFORE `--purpose reset`/RST confirmation.

For setup/reset/update/storage read [connections](references/connections.md). Read [operations](references/resource-operations.md) for hardware, leases/aliases/metadata. Only supported preset categories are public; detailed notes stay private, never justify write replay.

For panels/metrics read [workbench](references/workbench-and-metrics.md). Signed in: open bookmarked fixed home (`verified origin + /#/me`). First sign-in: authorized verified private handoff; `panel-link` ONCE, verify delivery, NEVER open/consume its one-use URL. The human confirms in browser and bookmarks `result.home`; a normal follow-up resumes. Metrics require approved origins/scoped grants; missing/stale/invalid samples are not zero or current percentages.
