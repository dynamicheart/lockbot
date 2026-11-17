---
name: lockbot-resource-agent
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
none exists, run `key-challenge --origin "$ORIGIN" --authorized` and give the user
the single chat confirmation step returned by the CLI. The request to use the
service authorizes this step even when the user does not mention a Key or a code.
Chat confirmation identifies the bot and restores the existing Key or creates a
first Key. For an installation-only request, stop after verifying the installation.

**When the user returns from the group.** Use `connections` to find the unfinished
request, then run `enrollment-status` with its enrollment directory. The local
`pending` label can remain after the user has confirmed in chat. If the service
reports `confirmed`, run `finish-enrollment` with the same directory and continue
the original task. The group confirmation already provided authorization.

**Explicit Key reset.** Once the user requests invalidation and acknowledges that other
Agents sharing the old Key must replace it, run
`key-challenge --origin "$ORIGIN" --purpose reset --authorized`, even when no Key is
saved locally. The `recover` subcommand uses a saved Key to find other eligible bindings.

## Quick start with a saved connection

Use Python 3.10 or newer. Run from the project root. Substitute the actual absolute path
of `scripts/lockbot_resource.py` beside this `SKILL.md` and run each CLI command directly.
Pass literal paths so a client's command allowlist can match the invocation.

```sh
python3 "/path/to/skill/scripts/lockbot_resource.py" connections
python3 "/path/to/skill/scripts/lockbot_resource.py" --connection "/path/to/connection.json" resources
python3 "/path/to/skill/scripts/lockbot_resource.py" --connection "/path/to/connection.json" command --operation lock --node node-01 --device 0 --device 1 --device-count 2 --model P800 --duration 30m
```

Use the matching private file listed by `connections`. Replace the
example node and cards with the exact IDs from the inventory. For a status question,
stop after `resources`. Use `command --help` for all write arguments.

- **Already signed into the workbench:** open the full bookmarked `/#/me` URL with
  authorized browser access. Reply with the address and what the page shows.
- **Needs to sign into the workbench:** use `panel-link` after verifying private
  delivery is authorized, then give the user the returned `result.login_url` in that
  private reply. The user opens and confirms it. Use `result.home` for return visits.
- **Acquire now:** use `lock` on an idle match with an empty queue. If none qualifies,
  report what is unavailable. Use `book` only when the user requests or accepts a queue.
- **Claim a booking:** use `command --operation claim --node <id>` when the user's
  booking is claimable. Add `--device` for a queued card. The booking sets the duration.
- **Renew a lease:** use `command --operation renew` with the time to add. The inventory
  lists server operations. The CLI uses server `lock` for both `claim` and `renew`.
- **Cards together:** put every requested card on that node in one command, repeating
  `--device`. Pass `--device-count` when the user specifies a quantity. If the whole
  selection cannot be made, report the shortfall before sending a write.
- **Whole node:** omit `--device`. If the user specifies a model, pass `--model` in
  either mode. Use `--minimum-model-count` for a whole-node hardware minimum.
  An alias does not establish the model. Stop if the model, target or freshness is unknown.
- **Named group:** pass the exact group as `command --group "$GROUP"`. If the user requests cards in a
  whole-node group, explain the mismatch and ask before switching group or allocation mode.
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
3. Use Python 3.10+ and the bundled CLI. Resolve `scripts/lockbot_resource.py`
   relative to this `SKILL.md` and run it by absolute path from the project directory.
   Run `--help` or `<subcommand> --help` for arguments and examples. With authorization to
   save the service, run `discover --destination <new-private-file>`. When the user gives
   you a Key file, run `init --key-file` first to save a verified connection, then pass
   that connection to resource commands. The service resolves the bot from the Key.
4. If the user asks to save a credential in this project, run from the project root and
   select `--scope project` for `init`, `key-challenge` or `recover`. Use only the project's
   catalog for a project-only setup. Default writes go to the system user's private
   catalog. Choose the command options yourself and report where the connection was saved.
5. To restore access, first look for an existing connection or personal Key on the
   approved service. Use it to recover other eligible groups without asking for names.
   A failed recovery stops at reporting the result. For an explicitly authorized reset,
   follow the **Explicit Key reset** command above.
   Quote the CLI's `user_instruction_zh` for Chinese or `user_instruction` for English
   verbatim. For other languages, translate `user_instruction` and preserve the exact
   `bind_command`. The verified chat confirmation identifies the bot.
   After chat confirmation, run `finish-enrollment --enrollment-dir "$ENROLLMENT_DIR"`.
   A confirmed reset has already taken effect. If the user asks to continue in a new
   session, their request and `chat_action: reset_applied` authorize saving the result.
   Run `finish-enrollment` directly without asking them to confirm the reset again.
6. In a new session run `connections` from the project root. Prefer the project catalog
   `.lockbot/connections/` over `~/.config/lockbot/connections/`. For project-only lookup,
   use `connections --directory .lockbot/connections`.
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
   If a name may refer to multiple groups or the user asks you to
   choose, check each eligible authorized group's live resources before asking or choosing.
3. An idle match with an empty queue supports `lock` even if `claimable_by_you` is false.
   A booking is a queue position. A lock or claim creates a lease.
   For a claim, match a booking marked `claimable_by_you` and omit the
   duration. For a renewal, pass the time to add to the existing lease. Release only the
   exact resource, without a duration.
   For card selections with different device IDs on different nodes, run a separate
   command for each node. Stop if any write is unconfirmed.
4. Send the verified write once and verify the resulting resource state. If the CLI
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
