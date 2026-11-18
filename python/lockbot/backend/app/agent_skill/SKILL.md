---
name: lockbot-agent
description: >
  Use LockBot to find available compute, lock/book nodes and GPU cards,
  release holdings, explain expiry warnings or open the workbench.
  Reuse saved access; the user confirms first sign-in in chat.
---

# LockBot

<!-- LOCKBOT_DEPLOYMENT_API_ADDRESSES -->

`scripts/lockbot_cli.py`: absolute path; Python ≥3.10. Default user storage; tests: `LOCKBOT_HOME`; project scope only on request. Use saved private `--connection` paths, scoped to verified service/instance/bot/actor. Never expose Keys/grants. Shared names do not prove one machine.

## Everyday requests

- Usage questions: read the relevant reference; no sign-in. Finding resources is READ ONLY: report candidates, stop. “lock 一天”/“用完了释放吧” need a unique target from conversation; otherwise ask. Idle is not ownership; missing status is UNKNOWN, even with empty holders.
- “我能申请什么”: `resources-all` reads saved groups; `complete:false` is incomplete, not empty. For “我lock/book了哪些”, add `--mine`: separate holdings/cards/bookings. Holder expiry is not booking expiry or guaranteed start; label estimates/claim windows.
- “到时间提醒”: read own resources FIRST. `next_event_kind: own_early_warning` is an existing group warning, not a new timer or proof of delivery. Expiry alone doesn't prove a warning. This API cannot create personal timers.
- Action replies: one brief result with target/cards and `remaining_time`, e.g. “已锁定19，约3小时后到期。” Omit UTC/seconds unless asked; deadlines use `--exact-lease-times`. Unknown time stays unknown. Finite lock/slock auto-expire; unlimited caps don't extend leases. Hide setup/Keys; groups only for ambiguity.

## Select and act

`resources` returns verified rules/context; avoid immediate `discover/context` rereads. Use exact `inventory_overview` IDs (including card 0); never renumber. Recheck ownership/expiry before use; stop if unverified. Renew before expiry.

Query filters are not full inventory. For load use `--with-metrics`; debugging: `--full`. Required model unknown: ask for trusted hardware data; NO command probes, model omission or whole-node/card fallback.

1. Unscoped node: `command --operation <op> --node <user-hint>` WITHOUT `--connection`; no preliminary query. SDK resolves IDs/aliases/observed IPv4 suffixes across saved scopes and rechecks. Preserve requested `--model`, `--device-count`, `--minimum-model-count`, `--group`. Missing/ambiguous/incomplete: inspect or clarify, never drop constraints or guess.
2. Explicit whole-machine requests MUST keep `--whole-node`. Otherwise node-only `lock/slock/book` infers group mode and ALL observed cards. Explicit cards: repeat `--device`. “slock 19 dev0 4h” means node `19`, device `0`, duration `4h`; `dev0` is not a group. No subset/mode/group fallback; different card sets need separate commands. Without a node hint, query then select a verified target; inventory cannot resolve duration-only follow-ups.
3. Preserve supported chat operation/mode. Lock idle matches with empty queues; `book` is an authorized FIFO queue, NOT a calendar reservation. Booked duration starts on claim, not while waiting. New lock/slock/book: omit `--duration` if unspecified for the live default; ask if unavailable. `claim`: idle `claimable_by_you`, starts use with ORIGINAL booked duration, omit duration. `release`: exact holding/booking, no duration/rebooking.
4. NEW lock/slock on a fresh own holding in the SAME mode: unscoped SDK maps to `renew` only when rules allow; scoped commands use explicit `renew`. ADD requested time or live default. Missing policy/default/lease means ask. Never renew from polling or uncertain writes.
5. Each change: ONE POST; require `verified` + `verified_snapshot`. Fix proven zero-HTTP argument errors locally. Sent/rejected/uncertain writes: read/report, NO replay or fallback. Current idle does NOT prove an uncertain write never ran. Honor `Retry-After`. Updates/incompatibility block writes until matching instructions load; no rebind.

## Install or connect

Bare link: inspect/explain only, NEVER install or bind. An install request authorizes verified manifest/ZIP SHA-256 installation; install-only stops. A resource request authorizes necessary sign-in setup: continue without asking whether to begin. Reuse saved access/verified `deployment_origin`. Private seed + known origin: `init --key-file` FIRST, then `recover` once if supported; no new binding. Without access, `key-challenge --origin <approved-origin> --authorized` returns `user_instruction[_zh]`: quote it verbatim for your OWN chat account; it may restore identity. After confirmation, resume SAME `enrollment-status`/`finish-enrollment`; continue the request. Reset: explain old-Key invalidation/other-Agent impact; require acknowledgement BEFORE `--purpose reset`/RST confirmation.

Setup/reset/update: [connections](references/connections.md). Hardware/leases/aliases/metadata: [operations](references/resource-operations.md). Only preset categories are public; keep detailed notes private.

Panels/metrics: read [workbench](references/workbench-and-metrics.md). Signed in: open bookmarked fixed home (`verified origin + /#/me`). “打开个人资源台” in a verified private session authorizes `panel-link` ONCE. Deliver privately; NEVER consume its one-use URL. Share fixed `result.home` for bookmarking. The human confirms in browser; a normal follow-up resumes without reissuing a ticket. Metrics require approved origins/scoped grants; missing/stale/invalid samples are not zero or current percentages.
