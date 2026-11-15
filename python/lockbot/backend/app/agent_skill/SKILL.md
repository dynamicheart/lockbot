---
name: lockbot-resource-agent
description: >
  Connect to a LockBot resource group, find saved connections, check shared compute,
  lock or book nodes and GPU cards, release holdings, or open the personal workbench.
  Uses the LockBot Resource API (lockbot.resource-api) with scoped lbr_ credentials.
  The user confirms new enrollment in their group chat.
---

# LockBot Resource Agent

- A LockBot service hosts bots. Each bot has its own inventory and rules, and a personal
  Key represents one verified chat identity on that bot. The same node name can occur on
  several bots. Select the connection before choosing a target.
- Whole-node bots allocate machines. Card bots allocate devices on a node. Use the bot's
  live inventory and rules to identify what the user asked for.
- A booking is a place in the queue. A successful lock or claim creates a holding with
  an expiry. Read back the resulting state before using the resource.
- Utilization reports what the hardware is doing. Read occupancy and queue state to
  decide whether LockBot can allocate it.

Keep credentials scoped to the verified service, instance and bot. Keep Key values and
grants out of replies, command arguments, Skill files and shared memory.

<!-- LOCKBOT_DEPLOYMENT_API_ADDRESSES -->

Open only the linked reference needed for the current request.

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
3. Use Python 3.10+ and the bundled CLI, `python3 scripts/lockbot_resource.py`.
   Run it with `--help` or `<subcommand> --help` for arguments and examples. With authorization to save the
   service, use `discover --destination <new-private-file>` to record the origin and instance. For an existing
   Key file, use `init --key-file` to verify the identity and save a connection reference.
   The user need not supply a bot ID. Keep the Key file in its private location.
4. If the user asks to save a credential in this project, run from the project root and
   select `--scope project` for `init`, `enroll` or `recover`. Use only the project's
   catalog for a project-only setup. Default writes go to the system user's private
   catalog. Choose the command options yourself and report where the connection was saved.
5. For a new connection without a Key, ask only for a missing group name or authorization
   to request a code. Then call `enroll` once, give the user its `@bot bind <code>`
   instruction, and wait for them to send it in that group. Call `finish-enrollment`
   on the same directory after they confirm. Use the resulting connection only after
   verified success. Read the saved pending state before considering another enrollment.
6. In a new session run `connections` from the project root. Prefer the matching record
   in `.lockbot/connections/` before `~/.config/lockbot/connections/`. For project-only
   lookup use `connections --directory .lockbot/connections`. If multiple services match
   and the user has not selected one, show their addresses and ask which to use. Use
   `--connection <private-file> context` to verify the intended identity and bot.
   Use `recover` for other eligible bindings before asking the user to rebind or rotate.

Read [connections and updates](references/connections.md) for setup commands, directory
rules, Key recovery, and version checks. An update or incompatible API blocks writes until
the matching instructions are loaded. Readbacks of earlier writes may continue.

## Query, allocate, and verify

1. Use `resources` with the verified connection. Read-only requests never submit a
   resource command. Label results with the service address when several sources are in play.
2. Before a write, verify `snapshot_fresh_for_write` and match the user's intent, exact
   node or card IDs, model, quantity, duration, allocation mode and any same-node condition.
   If the snapshot stays stale, the target is ambiguous, or required hardware metadata
   is unknown, explain what is missing. Never use a node alias or group title as model
   evidence. If the user delegates that choice, list the eligible authorized groups and
   check each group's live resources before choosing among them.
3. When asked to acquire now, use `lock` for an idle match with an empty queue, even if
   `claimable_by_you` is false. Use `book` only when the user requests or accepts a
   reservation. For a claim, match a booking marked `claimable_by_you` and omit the
   duration. For a renewal, pass the time to add to the existing lease. Release only the
   exact resource, without a duration.
4. Send the verified write once and verify the resulting resource state. When its outcome
   is uncertain, read current resources, report what is verifiable, and do not repeat the
   write. Honor server `Retry-After` responses when polling.
5. Recheck ownership and expiry while using a resource. A booking is not ownership.
   Stop resource use when ownership or a valid lease cannot be verified. Read any registered
   login address from the currently held resource's `node_access`, where a card inherits
   its parent node's address. Ask the operator if connection information is unavailable.
6. Public task notes use a supported preset category. Treat detailed purposes, Agent
   labels and nested task data as private. Never replay a command to add notes.

Read [resource operations and leases](references/resource-operations.md) for hardware notes,
aliases, lease timing, polling and task metadata. Read [HTTP fallback and errors](references/http-api.md)
when the bundled CLI cannot complete the requested operation. Use an authorized HTTP tool
with the same approved-origin, actor, freshness and verification checks.

## Workbench and utilization

For sign-in, verify the service and connection, issue `panel-link`, and deliver the link in
the verified private channel. The user opens and confirms it in their browser. Give them
the fixed workbench home `/#/me` to bookmark. When a private delivery tool confirms delivery,
leave the sign-in link out of the final reply. Do not open or consume it for the user.

For utilization, use only the approved metrics origin and scoped short-lived grant. Distinguish
a confirmed zero from missing, stale, failed and invalid samples. Use only current, valid
readings for percentages. Occupancy and hardware utilization are distinct observations.

Read [workbench, utilization and private reports](references/workbench-and-metrics.md) when
the user asks to sign in, compare utilization, share a resource page or use a granted private
report.
