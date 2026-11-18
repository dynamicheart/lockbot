---
name: lockbot-agent
description: >
  Use LockBot to find available compute, lock/book nodes and GPU cards,
  release holdings, explain expiry warnings or open the workbench.
  Reuse saved access; the user confirms first sign-in in chat.
---

# LockBot

<!-- LOCKBOT_DEPLOYMENT_API_ADDRESSES -->

`scripts/lockbot_cli.py`: absolute path, Python ≥3.10. `resources-all` reads saved groups; add `--mine` for own holdings/bookings. `complete:false` means incomplete, not empty. Use private `--connection` locators. Keep verified service/instance/bot/actor scoped. Same names across bots don't prove one machine. No Keys/grants in replies, args, Skills/memory. User storage by default; no invented paths. Test isolation: `LOCKBOT_HOME`; project scope only on request.

## Everyday requests

- Usage questions: read the relevant reference; no sign-in. Finding resources is READ ONLY: read eligible groups, report choices, stop. Follow-ups like “lock 一天”/“用完了释放吧” need a unique conversational target; otherwise ask. Inventory cannot choose it.
- “我能申请什么”: `recover` once from a verified seed if supported, then `resources-all`. For “我lock/book了哪些”, add `--mine`: separate holdings/cards/bookings. Booking is not ownership; holder time isn't booking expiry/guaranteed start. Label estimates/claim windows.
- “到时间提醒”: read own resources FIRST. `next_event_kind: own_early_warning` is an existing group warning, not a new timer or proof of delivery. Expiry alone doesn't prove a warning. This API cannot create personal timers.
- Reply with results or needed action; use `remaining_time`; deadlines use `--exact-lease-times`. Missing time is UNKNOWN, never unlimited. Finite leases, including slock, auto-expire; unlimited duration caps don't extend them. Hide Key/paths/setup mechanics; group names only for ambiguity.

## Select and act

`resources` returns verified rules/context; avoid immediate `discover/context` rereads. Use exact `inventory_overview` node/card IDs (including 0), never renumber. Recheck later ownership/expiry; stop workload use if unverifiable.

Query `--model/--node` filters are not full inventory. Use `--with-metrics` for load questions, `--full` for debugging. Unknown hardware stays unknown.

1. Resolve exact IDs, aliases or observed IPv4 last octets across authorized groups (19 never matches 119). Aliases prove no address; names prove no hardware. Require `snapshot_fresh_for_write`; verify model, quantity, mode, duration and same-node constraints. Stale/ambiguous/unverified means NO command. Pass requested `--model`, `--device-count`, `--minimum-model-count` and named `--group`.
2. Node-only `lock/slock/book`: whole-node groups use `--whole-node`; card groups enumerate ALL observed `--device`s in ONE command. No silent subset: report conflicts/shortfalls. Never turn card requests into whole nodes/change groups. Different card sets need separate node commands; stop on uncertainty.
3. Preserve supported chat operation/mode. Lock idle matches with empty queues; `book` is an authorized FIFO queue, NOT a calendar reservation. Booked duration starts on claim, not while waiting. New lock/slock/book: omit `--duration` if unspecified for the live default; ask if unavailable. `claim`: idle `claimable_by_you`, starts use with ORIGINAL booked duration, omit duration. `release`: exact holding/booking, no duration/rebooking.
4. A NEW explicit lock/slock on a fresh verified own holding in the SAME mode maps to CLI `renew`: ADD requested time or verified live group default (else ask), only if relock allowed. Never renew from polling/time passage/uncertain writes. Mixed/unknown modes stop.
5. Each intended change: ONE POST; require `verified` + `verified_snapshot`. Fix proven zero-HTTP argument errors locally. Sent/uncertain writes: read/report, NO replay/alternate target/HTTP bypass. Honor `Retry-After`. Updates/incompatibility block writes until matching instructions load; no rebind.

## Install or connect

Bare link: inspect only; inert ZIP inspection may locate its manifest. Explicit install: verify manifest/complete ZIP SHA-256, install/reload; install-only stops. Reuse a connection/verified installed `deployment_origin`, no backup/known-URL demand. With a privately supplied seed Key and known origin but no connection, `init --key-file` then `recover`; no new binding. Without access, `key-challenge --origin <approved-origin> --authorized` returns `user_instruction[_zh]`: quote it verbatim for your OWN chat account; it may restore identity. After confirmation, resume SAME pending enrollment via `enrollment-status`/`finish-enrollment`; fulfill normal follow-ups. Reset: explain old-Key invalidation/other-Agent impact and obtain acknowledgement BEFORE `--purpose reset`/RST confirmation.

For setup/reset/update read [connections](references/connections.md). Read [operations](references/resource-operations.md) for hardware, leases/aliases/metadata. Only preset categories are public; detailed notes stay private, never justify write replay.

Panels/metrics: read [workbench](references/workbench-and-metrics.md). Signed in: open bookmarked fixed home (`verified origin + /#/me`). In a verified private session, “打开个人资源台” authorizes its necessary sign-in handoff: `panel-link` ONCE, verify delivery, NEVER open/consume its one-use URL. The human confirms in browser and bookmarks `result.home`; a normal follow-up resumes. Metrics require approved origins/scoped grants; missing/stale/invalid samples are not zero or current percentages.
