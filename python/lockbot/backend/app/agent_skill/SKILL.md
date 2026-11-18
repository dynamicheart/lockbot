---
name: lockbot-agent
description: >
  Use LockBot to find available compute, lock/book nodes and GPU cards,
  release holdings, explain expiry warnings or open the workbench.
  Reuse saved access; the user confirms first sign-in in chat.
---

# LockBot

<!-- LOCKBOT_DEPLOYMENT_API_ADDRESSES -->

`scripts/lockbot_cli.py`: absolute path, Python ≥3.10. Private user storage; tests: `LOCKBOT_HOME`; project only on request. Saved `--connection` pins service/instance/bot/actor. No Keys/grants in replies. Equal names need not mean one machine.

## Everyday requests

- Usage questions: read reference, no sign-in. Search is READ ONLY: report, stop. “lock 一天”/“用完了释放吧” need a unique prior target; else ask. Idle is not ownership; missing status is UNKNOWN, even with empty holders.
- “我能申请什么”: `resources-all` reads saved groups; `complete:false` is incomplete, not empty. For “我lock/book了哪些”, add `--mine`: separate holdings/cards/bookings. Holder expiry is not booking expiry or guaranteed start; label estimates/claim windows.
- “到时间提醒”: read own resources FIRST. `next_event_kind: own_early_warning` is an existing group warning, not a new timer or proof of delivery. Expiry alone doesn't prove a warning. This API cannot create personal timers.
- Reply briefly with target/cards and `remaining_time`: “已锁定19，约3小时后到期。” UTC/seconds only if asked; deadlines use `--exact-lease-times`. Unknown time stays unknown. Finite leases auto-expire; unlimited caps don't extend them. Hide setup/Keys; groups only for ambiguity.

## Select and act

`resources` returns verified rules/context; avoid immediate `discover/context` rereads. Use exact `inventory_overview` IDs (including card 0); never renumber. Recheck ownership/expiry before use; stop if unverified. Renew before expiry.

Query filters are not full inventory. For load use `--with-metrics`; debugging: `--full`. Required model unknown: ask for trusted hardware data; NO command probes, model omission or whole-node/card fallback.

1. Unscoped node: `command --operation <op> --node <user-hint>` WITHOUT `--connection`; no preliminary query. SDK resolves IDs/aliases/observed IPv4 suffixes across saved scopes and rechecks. Preserve requested `--model`, `--device-count`, `--minimum-model-count`, `--group`. Missing/ambiguous/incomplete: clarify; keep constraints.
2. Explicit whole-machine requests MUST keep `--whole-node`. Otherwise node-only `lock/slock/book` infers group mode and ALL observed cards. Explicit cards: repeat `--device`. “slock 19 dev0 4h” means node `19`, device `0`, duration `4h`; `dev0` is not a group. No subset/mode/group fallback; different card sets need separate commands. Without a node hint, query then select a verified target; inventory cannot resolve duration-only follow-ups.
3. Preserve supported chat operation/mode. Lock idle matches with empty queues; `book` is an authorized FIFO queue, NOT a calendar reservation. Booked duration starts on claim, not while waiting. New lock/slock/book: omit `--duration` if unspecified for the live default; ask if unavailable. `claim`: idle `claimable_by_you`, starts use with ORIGINAL booked duration, omit duration. `release`: exact holding/booking, no duration/rebooking.
4. NEW lock/slock on a fresh own holding in the SAME mode: unscoped SDK maps to `renew` only when rules allow; scoped commands use explicit `renew`. ADD requested time or live default. Ask if policy/lease or both duration/default are unknown. Never renew from polling or uncertain writes.
5. Each change: ONE POST; require `verified` + `verified_snapshot`. Fix proven zero-HTTP argument errors locally. Unknown: quote `message_zh`, run read-only `next_command`, report and STOP; no retry offer, connection switch or fallback. Idle/absent never proves failure. Rejected writes: read/report, NO replay. Honor `Retry-After`; updates block writes until matching instructions load; no rebind.

## Install or connect

Bare link: inspect/explain only, NEVER install or bind. An install request authorizes verified manifest/ZIP SHA-256 installation; install-only stops. A resource request authorizes necessary sign-in setup: continue without asking whether to begin. Reuse saved access/verified `deployment_origin`. Private seed + known origin: `init --key-file` FIRST; follow `result.next_command` once to recover supported groups BEFORE resource queries; no new binding. Without access, `key-challenge --origin <approved-origin> --authorized` returns `user_instruction[_zh]`: quote it verbatim for your OWN chat account; it may restore identity. After confirmation, resume SAME `enrollment-status`/`finish-enrollment`; continue the request. Reset: explain old-Key invalidation/other-Agent impact; require acknowledgement BEFORE `--purpose reset`/RST confirmation.

[Setup/reset/update](references/connections.md); [hardware/leases](references/resource-operations.md). Only preset categories are public; details stay private.

Panels/metrics: read [workbench](references/workbench-and-metrics.md). “打开资源台”: after sign-in, use browser to open fixed home/bookmark; API reads do NOT open pages. Authorized first handoff in verified private chat: `panel-link` ONCE; deliver `login_url` privately, NEVER consume it. Include fixed `result.home` for bookmarking; human confirms in browser. Failure: follow `home`/`next_step`, no retry/reissue. No browser: give fixed home, say it was not opened. Metrics need approved origins/scoped grants; missing/stale/invalid is not zero/current.
