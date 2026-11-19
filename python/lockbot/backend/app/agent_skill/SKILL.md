---
name: lockbot-agent
description: >
  Use LockBot to find available compute, lock/book nodes and GPU cards,
  release holdings, explain expiry warnings or open the workbench.
  Reuse saved access; the user confirms first sign-in in chat.
---

# LockBot

<!-- LOCKBOT_DEPLOYMENT_API_ADDRESSES -->

`scripts/lockbot_cli.py`: absolute path, Python ≥3.10. Private user storage; tests: `LOCKBOT_HOME`; project only on request. `--connection` pins service/instance/bot/actor. No Keys/grants in replies.

## Everyday requests

- Usage questions: read reference, no sign-in. Search-only: read, report, stop; search-and-lock may act. “lock 一天”/“用完了释放吧” need a unique prior target; else ask. Missing hardware/state/queues: report UNKNOWN; idle isn't ownership or physical non-use.
- “我能申请什么”/“找台P800”: `resource list --limit 5 --json node,unit,hardware,states`; add `--model P800` when requested. Own lock/book: add `--mine`, keeping holdings/bookings separate. All/own: follow `next_command` to exhaustion; state partial coverage. See [queries](references/resource-queries.md). No bot/group IDs needed.
- “到时间提醒”: read own resources FIRST. `next_event_kind: own_early_warning` is an existing group warning, not a new timer/proof of delivery. Expiry alone doesn't prove a warning; API cannot create personal timers.
- Briefly give target/cards and `remaining_time`: “已锁定19，约3小时后到期。” UTC/seconds only if asked; deadlines: `--exact-lease-times`. Unknown time stays unknown. Finite leases auto-expire; unlimited caps don't extend them. Hide setup/Keys; groups only for ambiguity.

## Select and act

Reuse returned facts. Use `ref` with `resource view REF` or `command --ref REF` to distinguish repeated names. `resource devices REF` returns exact card IDs (including 0); never renumber. Reads verify context; avoid immediate `discover/context` rereads. Recheck ownership/expiry before use; stop if unverified. Renew before expiry. Holder expiry isn't booking expiry/guaranteed start; label estimates/claim windows.

Query filters are not full inventory. Add `--with-metrics` only for load questions; verify freshness; zero is not availability. Required model unknown: ask for trusted hardware data; NO command probes, model omission or whole-node/card fallback.

1. Node hint: `command --operation <op> --node <hint>` WITHOUT `--connection` or preliminary query. SDK resolves IDs/aliases/IPv4 suffixes across saved scopes and rechecks. Preserve `--model`, `--device-count`, `--minimum-model-count`, `--group`. Missing/ambiguous/incomplete: clarify; keep constraints.
2. Whole-machine requests keep `--whole-node`; otherwise node-only `lock/slock/book` infers group mode and ALL observed cards. Explicit cards: repeat `--device`. “slock 19 dev0 4h”: node `19`, device `0`, duration `4h`; `dev0` isn't a group. No subset/mode/group fallback; different card sets need separate commands. Without a node hint, query then select a verified target.
3. Preserve supported chat operation/mode. Lock idle matches with empty queues; `book` is an authorized FIFO queue, NOT a calendar reservation. Booked duration starts on claim, not while waiting. New lock/slock/book: omit `--duration` if unspecified for the live default; ask if unavailable. `claim`: idle `claimable_by_you`, starts use with ORIGINAL booked duration, omit duration. `release`: exact holding/booking, no duration/rebooking.
4. Fresh own holding in the SAME mode: unscoped lock/slock maps to `renew` only when allowed; scoped commands use `renew`. ADD requested time or live default. Ask if policy/lease or both duration/default are unknown. Never renew from polling or uncertain writes.
5. ONE POST per change; require `verified` + `verified_snapshot`. Fix proven zero-HTTP argument errors locally. Unknown: quote `message_zh`, run read-only `next_command`, report and STOP; no retry/switch/fallback. Idle/absent never proves failure. Rejected writes: read/report, NO replay. Honor `Retry-After`; updates block writes until matching instructions load; no rebind.

## Install or connect

Bare link: inspect/explain only, NEVER install or bind. An install request authorizes verified manifest/ZIP SHA-256 installation; install-only stops. A resource request authorizes necessary sign-in setup: continue without asking whether to begin. Reuse saved access/verified `deployment_origin`. Private seed + known origin: `init --key-file` FIRST; follow `result.next_command` once to recover supported groups BEFORE resource queries; no new binding. Without access, `key-challenge --origin <approved-origin> --authorized` returns `user_instruction[_zh]`: quote it verbatim for your OWN chat account; it may restore identity. After confirmation, resume SAME `enrollment-status`/`finish-enrollment`; continue the request. Reset: explain old-Key invalidation/other-Agent impact; require acknowledgement BEFORE `--purpose reset`/RST confirmation.

[Setup/reset/update](references/connections.md); [hardware/leases](references/resource-operations.md). Only preset categories are public; details stay private.

Panels/metrics: read [workbench](references/workbench-and-metrics.md). “打开资源台”: browser-open fixed home/bookmark; no browser: provide it, say not opened. First sign-in in verified private chat: `panel-link` ONCE, deliver `login_url` privately with `result.home` for bookmarking; human confirms. NEVER consume the ticket. Failure: follow `home`/`next_step`, no reissue. Metrics need approved origins/scoped grants; missing/stale is not zero/current.
