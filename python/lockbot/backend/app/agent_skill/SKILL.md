---
name: lockbot-agent
description: >
  Use LockBot to find available compute, lock/book nodes and GPU cards,
  release holdings, explain expiry warnings or open the workbench.
  Reuse saved access; the user confirms first sign-in in chat.
---

# LockBot

<!-- LOCKBOT_DEPLOYMENT_API_ADDRESSES -->

`scripts/lockbot_cli.py`: absolute path, Python ≥3.10. `resources-all` reads saved groups; add `--mine` for own holdings/bookings. `complete:false` means incomplete, not empty. Use private `--connection` paths. Keep verified service/instance/bot/actor scoped. Shared names don't prove one machine. No Keys/grants in replies, args, Skills/memory. Default user storage; no invented paths. Tests: `LOCKBOT_HOME`; project scope only on request.

## Everyday requests

- Usage questions: read the relevant reference; no sign-in. Finding resources is READ ONLY: read eligible groups, report choices, stop. Follow-ups like “lock 一天”/“用完了释放吧” need a unique conversational target; otherwise ask. Inventory cannot choose it.
- “我能申请什么”: `recover` once from a verified seed if supported, then `resources-all`. For “我lock/book了哪些”, add `--mine`: separate holdings/cards/bookings. Booking is not ownership; holder time isn't booking expiry/guaranteed start. Label estimates/claim windows.
- “到时间提醒”: read own resources FIRST. `next_event_kind: own_early_warning` is an existing group warning, not a new timer or proof of delivery. Expiry alone doesn't prove a warning. This API cannot create personal timers.
- Action replies: one brief result with target/cards and `remaining_time`, e.g. “已锁定19，约3小时后到期。” Omit UTC, seconds and verification jargon unless asked; deadlines use `--exact-lease-times`. Unknown time stays unknown. Finite lock/slock auto-expire; unlimited caps don't extend leases. Hide setup/Keys; groups only for ambiguity.

## Select and act

`resources` returns verified rules/context; avoid immediate `discover/context` rereads. Use exact `inventory_overview` IDs (including card 0); never renumber. Recheck later ownership/expiry; stop workload use if unverifiable.

Query `--model/--node` filters are not full inventory. Use `--with-metrics` for load questions, `--full` for debugging. Required model unknown: STOP before command; never probe or retry without that model.

1. Unscoped node: `command --operation <op> --node <user-hint>` WITHOUT `--connection`; no preliminary query. SDK resolves exact ID/alias/observed IPv4 suffix across saved scopes, rechecks before writing. Preserve requested `--model`, `--device-count`, `--minimum-model-count`, `--group`. Missing/ambiguous/incomplete: inspect or clarify, never drop constraints or guess.
2. Node-only `lock/slock/book`: SDK infers whole-node vs ALL observed cards from verified group mode. Explicit cards: repeat `--device`. “slock 19 dev0 4h” means node `19`, device `0`, duration `4h`; `dev0` is not a group. One command, no silent subset or changed mode/group; different card sets need separate commands. Without a node hint, query then select a verified target; inventory cannot resolve duration-only follow-ups.
3. Preserve supported chat operation/mode. Lock idle matches with empty queues; `book` is an authorized FIFO queue, NOT a calendar reservation. Booked duration starts on claim, not while waiting. New lock/slock/book: omit `--duration` if unspecified for the live default; ask if unavailable. `claim`: idle `claimable_by_you`, starts use with ORIGINAL booked duration, omit duration. `release`: exact holding/booking, no duration/rebooking.
4. A NEW explicit lock/slock on a fresh own holding in the SAME mode maps to `renew` if relock is permitted: ADD requested time or verified live default. Missing default, permission or mode means ask. Never renew from polling, time passage or uncertain writes.
5. Each change: ONE POST; require `verified` + `verified_snapshot`. Fix proven zero-HTTP argument errors locally. Sent/uncertain writes: read/report, NO replay/alternate target/HTTP bypass. Honor `Retry-After`. Updates/incompatibility block writes until matching instructions load; no rebind.

## Install or connect

Bare link: inspect only; ZIP inspection may locate its manifest. Install: verify manifest/ZIP SHA-256, install/reload; install-only stops. Reuse saved access/verified installed `deployment_origin`. Private seed Key + known origin: `init --key-file`, then `recover`; no new binding. Without access, `key-challenge --origin <approved-origin> --authorized` returns `user_instruction[_zh]`: quote it verbatim for your OWN chat account; it may restore identity. After confirmation, resume SAME `enrollment-status`/`finish-enrollment`; continue the request. Reset: explain old-Key invalidation/other-Agent impact; require acknowledgement BEFORE `--purpose reset`/RST confirmation.

For setup/reset/update read [connections](references/connections.md). Read [operations](references/resource-operations.md) for hardware, leases/aliases/metadata. Only preset categories are public; keep detailed notes private.

Panels/metrics: read [workbench](references/workbench-and-metrics.md). Signed in: open bookmarked fixed home (`verified origin + /#/me`). In a verified private session, “打开个人资源台” authorizes its necessary sign-in handoff: `panel-link` ONCE, verify delivery, NEVER open/consume its one-use URL. The human confirms in browser and bookmarks `result.home`; a normal follow-up resumes. Metrics require approved origins/scoped grants; missing/stale/invalid samples are not zero or current percentages.
