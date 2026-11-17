---
name: lockbot-agent
description: >
  Connect to a LockBot resource group, find saved connections, check shared compute,
  lock or book nodes and GPU cards, release holdings, or open the personal workbench.
  Uses the LockBot Resource API (lockbot.resource-api) with scoped lbr_ credentials.
  The user confirms new enrollment in their group chat.
---

# LockBot Resource Agent

LockBot manages shared compute for chat groups. It tracks who holds each machine or
accelerator card, when the lease expires, and who is queued. A LockBot service can host
several resource groups. Each group has its own bot, inventory, and allocation rules.

The bundled CLI verifies your group identity, reads the live inventory, and checks
resource writes. A personal `lbr_` Key identifies both you and your bot on its service.

Keep credentials scoped to the verified service, instance and bot. Keep Key values and
grants out of replies, command arguments, Skill files and shared memory.

<!-- LOCKBOT_DEPLOYMENT_API_ADDRESSES -->

**First connection or missing Key.** If the user asks to use an identified LockBot
service (for example, to request a machine), look for a saved connection first. If
none exists, use `key-challenge --origin "$ORIGIN" --authorized` to obtain the
single chat confirmation command. This prepares a short-lived request. The service
connects the account only when the user sends the command from their own chat account
in their chosen group. Give that command directly when the user asks to check or
request resources on the service. For installation only, stop after verifying the
installation. After confirmation, use the service's `outcome` to say the original
sign-in was restored or a new connection was established. If the service omits the
outcome, say the account is connected.

**Continue after group confirmation.** If the user asks to use the same service
(for example, “帮我看看资源”), use `connections` and check its unfinished request
with `enrollment-status`. The local `pending` label can remain after chat confirmation.
When the service reports `confirmed`, `finish-enrollment` verifies and stores the
already-confirmed sign-in locally. Use the same enrollment directory, then fulfill the
current request. If the user only asks for resources, respond with the resource status.

**Explicit Key reset.** Once the user requests invalidation and acknowledges that other
Agents sharing the old Key must replace it, run
`key-challenge --origin "$ORIGIN" --purpose reset --authorized`, even when no Key is
saved locally. The `recover` subcommand uses a saved Key to find other eligible bindings.

## Quick start with a saved connection

Use Python 3.10 or newer. Run from the project root. Substitute the actual absolute path
of `scripts/lockbot_cli.py` beside this `SKILL.md` and run each CLI command directly.
Pass literal paths so a client's command allowlist can match the invocation.

```sh
python3 "/path/to/skill/scripts/lockbot_cli.py" connections
python3 "/path/to/skill/scripts/lockbot_cli.py" --connection "/path/to/connection.json" resources
python3 "/path/to/skill/scripts/lockbot_cli.py" --connection "/path/to/connection.json" command --operation lock --node node-01 --device 0 --device 1 --device-count 2 --model P800 --duration 30m
```

Use the matching private file listed by `connections`. Replace the
example node and cards with the exact IDs from the inventory. For a status question,
stop after `resources`. Its `context` includes identity, current rules and task-category
choices; do not issue separate `discover` or `context` calls for the same ordinary read.
`command` verifies its returned `verified_snapshot`; do not immediately read again just
to repeat that verification. Later ownership checks remain necessary.
Use `command --help` for all write arguments.

Use the CLI's formatted `remaining_time` for status, acquisition, claim and renewal
(for example, `2h 30m` becomes “约 2 小时 30 分钟后到期”). State each shared deadline once.
Use `resources --exact-lease-times` for an exact deadline or a timed stop or renewal.

- **Already signed into the workbench:** open the bookmarked fixed URL in one step.
  If the bookmark is not available to your tools, append `/#/me` to the selected
  connection's verified service origin. Pass the full URL on the first browser call
  and include it with the page findings in your reply. Skip `panel-link`.
- **Needs to sign into the workbench:** use `panel-link` once with authorization for private
  delivery. If a private delivery tool is available, use it and check its result before
  saying the link was sent. Otherwise provide the link only within the verified private
  handoff. Tell the user to open it and confirm the account, then bookmark `result.home`
  (include its full address). Wait for their confirmation before using the workbench.
  For this handoff, read only the instructions relevant to sign-in and the selected
  connection. Use a saved connection, or run `init` directly when the user already
  supplied an approved origin and a private Key file. `init` verifies the service.
  When the browser session is authenticated at the fixed entry, check holdings and
  bookings there. A normal follow-up request is enough to resume.
- **Find resources:** “帮我找台 P800 的机器” or “看看有哪些资源能用” asks for
  available choices. Read the inventory, report a suitable machine, and stop. Finding
  a match does not authorize locking or booking it. A follow-up such as “lock 一天”
  authorizes locking the uniquely selected machine for that duration.
- **My resources:** check every saved, authorized group. Separate current holdings
  from queued bookings, including individual cards; match the verified actor exactly.
- **Expiry reminders:** for “到时间提醒我”, first read the user's current resources.
  `next_event_kind: own_early_warning` describes the bot's existing scheduled group
  warning. Explain that schedule without claiming a new reminder was created or delivered.
  An expiry timestamp alone does not prove a warning is enabled. The resource API cannot
  create personal timers; state that limit when no existing warning is reported.
- **Acquire now:** a request such as “申请一台 P800 用两小时” asks for a lease now.
  Use `lock` on an idle match with an empty queue. If no match qualifies, report that
  and ask if the user wants to queue. Send `book` only after the user requests or
  accepts a queue.
- **Claim a booking:** use `command --operation claim --node <id>` when the user's
  booking is claimable. Pass `--device` for a queued card or `--whole-node` for an entire
  node. The booking sets the duration.
- **Renew a lease:** use `command --operation renew` with the time to add. The inventory
  lists server operations. The CLI uses server `lock` for both `claim` and `renew`.
- **Cards together:** put every requested card on that node in one command, repeating
  `--device`. Pass `--device-count` when the user specifies a quantity. If the whole
  selection cannot be made, report the shortfall before sending a write.
- **Whole node:** pass `--whole-node` only when the user requested or approved the
  entire node. If the user specifies a model, pass `--model` in either mode.
  Use `--minimum-model-count` for a whole-node hardware minimum. Stop if model, target
  or freshness is unknown. An alias does not establish the model.
- **Named group:** pass the user's exact group as `command --group "$GROUP"`.
  If they request cards in a whole-node group, stay read-only. Explain the mismatch
  and wait for the user to choose a different group or explicitly approve an entire node.
  An error is not permission to change the original group, mode or target.
- **Unconfirmed write:** read the current resources and report the result. Do not send
  another write for the same request.

## Install or connect

1. **A link or pasted Skill alone authorizes inspection only.** Read the public manifest
   without a Key, explain the Skill, and end the turn. Wait for the user to explicitly
   ask for installation before calling an installer or writing a saved connection.
   Use the full URL supplied by the user, the **Complete installation ZIP** named in the
   pasted text, or one unambiguous approved connection. Ask for the original URL if none
   is available. Example hosts in the references are placeholders.
2. Fetch `/api/agent-skill` on the approved origin. Resolve
   `installation.download_url` against it and verify the archive's SHA-256 before
   installing the complete ZIP with the client's installer. Verify the installed files
   and reload the Skill. If a previously installed helper rejects the archive layout,
   use the client's installer for the checked ZIP, then use the helper in the new package.
   A failed or unavailable installation remains unverified.
3. Use Python 3.10+ and the bundled CLI. Resolve `scripts/lockbot_cli.py`
   relative to this `SKILL.md` and run it by absolute path from the project directory.
   Run `connections` to find the user's existing sign-in. With authorization to save
   the service, run `discover --destination <new-private-file>`. During an explicit
   credential import, use `init --key-file` with the privately supplied file.
4. Save connections in the current user's private catalog by default. Respect
   `LOCKBOT_HOME` when set. Use `--scope project` only for a requested project-only setup.
   Handle credential locations internally. In ordinary replies, say what is connected
   and what the user needs to do. Ask the user to confirm in their group or browser
   when needed.
5. To restore access, first look for an existing connection or personal Key on the
   approved service. Use it to recover other eligible groups without asking for names.
   A failed recovery stops at reporting the result. For an explicitly authorized reset,
   follow the **Explicit Key reset** command above.
   Quote the CLI's `user_instruction_zh` for Chinese or `user_instruction` for English
   verbatim. For other languages, translate `user_instruction` and preserve the exact
   `bind_command`. The verified chat confirmation identifies the bot.
   After chat confirmation, run `finish-enrollment --enrollment-dir "$ENROLLMENT_DIR"`.
   `chat_action: reset_applied` confirms the group has already applied the reset.
   A request that needs access to the same service, including a read-only resource
   query in a new session, can use `finish-enrollment` to save that sign-in locally.
6. In a new session run `connections` to reuse saved sign-ins. With `LOCKBOT_HOME`,
   the default lookup reads only that catalog. For explicit project-only lookup, use
   `connections --directory .lockbot/connections`.
   - For a status request, run `resources` with the matching path from `result.connections`.
     If multiple services match and the user has not selected one, show their addresses
     and ask which to use.
   - When the user returns to finish a binding, take `enrollment_dir` from
     `pending_enrollments` and check it with `enrollment-status`. After chat confirmation,
     finish that enrollment using the same directory.

Read [connections and updates](references/connections.md) for setup commands, directory
rules, Key recovery, and version checks. An update or incompatible API blocks writes until
the matching instructions are loaded. Readbacks of earlier writes may continue.

## Query, allocate, and verify

1. Use `resources` with the verified connection. Read-only requests never submit a
   resource command. Label results with the service address when several sources are in play.
2. Before a write, verify `snapshot_fresh_for_write` and match the user's intent, exact
   node or card IDs, model, quantity, duration, allocation mode and any same-node condition.
   **Stop without calling `command`** if the snapshot stays stale, the target is ambiguous,
   or the requested hardware model cannot be verified. A node alias or group title does
   not prove its model. Pass `--model` so the CLI rechecks the chosen hardware before writing.
   Find the environment through saved, verified connections. If the user gives a short
   identifier such as `lock 19`, match it against the exact node ID, registered alias,
   or the last octet of an IPv4 address reported in authorized inventory. Resolve each
   identifier in a comma-separated list. Check eligible groups and ask only about
   candidates that remain ambiguous.
3. Preserve the requested operation (`lock`, `slock`, or `book`). A direct command such
   as `lock 19` requests the lock once the live inventory resolves the exact node.
   In a group that allocates whole nodes, a node target with no requested cards selects
   that whole node. Pass `--whole-node` for it. If the request omits duration, omit
   `--duration` for `lock`, `slock`, or `book` and let the CLI verify and use the group's
   current default. Ask for duration if the service cannot supply a usable default.
   When the user gives only a duration, use a uniquely selected resource from the
   current conversation or ask which resource they mean. An idle match with an empty queue supports `lock` even if `claimable_by_you` is false.
   A booking is a queue position. A lock or claim creates a lease.
   For a claim, match a booking marked `claimable_by_you` and omit the
   duration. For a renewal, pass the time to add to the existing lease. Release only the
   exact resource, without a duration.
   For card selections with different device IDs on different nodes, run a separate
   command for each node. Stop if any write is unconfirmed.
4. Send the verified write once and inspect its `verified_snapshot` and `verified` result. If the CLI
   reports an unconfirmed write, read current resources and report what is verifiable.
   Do not try another write for that request, including by
   changing the target or switching to HTTP. Honor server `Retry-After` when polling.
5. Recheck ownership and expiry while using a resource. A booking is not ownership.
   Stop resource use when ownership or a valid lease cannot be verified. Read any registered
   login address from the currently held resource's `node_access`, where a card inherits
   its parent node's address. Ask the operator if connection information is unavailable.
6. Public task notes use a supported preset category. Treat detailed purposes, Agent
   labels and nested task data as private. Never replay a command to add notes.

Read [resource operations and leases](references/resource-operations.md) for hardware notes,
aliases, lease timing, polling and task metadata.

## Workbench and utilization

For sign-in, verify the service and connection, issue `panel-link`, and deliver the link in
the verified private channel. The user opens and confirms it in their browser. Give them
the fixed workbench home `/#/me` to bookmark. When a private delivery tool confirms delivery,
leave the sign-in link out of the final reply. Do not open or consume it for the user.

For utilization, use only the approved metrics origin and scoped short-lived grant. Distinguish
a confirmed zero from missing, stale, failed and invalid samples. Use only current, valid
readings for percentages. Occupancy and hardware utilization are distinct observations.

Read [workbench, utilization and private reports](references/workbench-and-metrics.md) when
the user asks to open the workbench, compare utilization, share a resource page or use a
granted private report.
