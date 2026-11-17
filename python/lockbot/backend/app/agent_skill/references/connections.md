# Bundled CLI

From the project directory, invoke the bundled `scripts/lockbot_resource.py` beside the
installed `SKILL.md` directly. In examples, replace `$LOCKBOT_CLI` with the script's
absolute path and substitute the actual connection or enrollment paths before running.
`--help` lists commands, and
`<subcommand> --help` describes the arguments. Select an existing private connection
with `--connection <file>` before the subcommand.

```sh
python3 "$LOCKBOT_CLI" --help
python3 "$LOCKBOT_CLI" command --help
python3 "$LOCKBOT_CLI" connections
python3 "$LOCKBOT_CLI" --connection /private/connection.json resources
```

| Goal | Subcommand |
| --- | --- |
| Verify a server or install the ZIP | `discover` or `install` |
| Set up access with an existing Key file | `init` |
| Request a binding and finish after the user confirms | `key-challenge`, then `finish-enrollment` |
| Invalidate the old Key after explicit authorization and group confirmation | `key-challenge --purpose reset`, then `finish-enrollment` |
| Restore additional bindings using a verified existing Key | `recover` |
| Find or verify a saved connection | `connections` or `context` |
| Read status or use an exact resource | `resources` or `command` |
| Open the workbench or check utilization | `panel-link` or `metrics` |

## Install the complete ZIP and use the fixed client

<!-- LOCKBOT_DEPLOYMENT_API_ADDRESSES -->

For ongoing use, install the manifest's **`installation.download_url` ZIP**, not Markdown
alone: it contains `SKILL.md`, `references/`, `scripts/lockbot_resource.py` and `installation.json`.
One installation can serve multiple independently approved connections when its verified
`package_version` matches a service's declared package revision. Keep each connection's
origin, instance, bot, actor and key reference separate. Different rendered deployment
URLs alone do not require replacing the package; old servers remain conservative.

A ZIP URL does not automatically install in every client. Use its installer or verify the
manifest SHA-256 before extracting, then reload the Skill. Direct legacy API clients remain
supported; the helper is the recommended fixed path, not a new server requirement.

Run the installed helper with Python 3.10 or newer. Use `--help` to find commands. If Python or the installed helper cannot run, report that limitation. Choose exact resource IDs from live inventory. The helper verifies mutations and reports when the result is uncertain. Reconcile uncertainty with a read and keep following the lease expiry rules.

If the user requests a resource on the identified service and has no valid saved Key, run
`key-challenge --origin "$ORIGIN" --authorized` for chat confirmation on a
server that advertises `chat_credential_challenges`. Use the confirmation sentence from
[the setup workflow](../SKILL.md#install-or-connect). Use the saved `enrollment_dir` with
`finish-enrollment` to verify and save the connection after confirmation. If no
Key is active for that user and bot, the same confirmation creates one and
reports `outcome: created`. If an active Key cannot be recovered, report that
result and keep it valid.

Use `key-challenge --origin "$ORIGIN" --purpose reset --authorized` after the user
explicitly chooses to invalidate the old Key and acknowledges that other Agents sharing
it will need the replacement. This works without a locally saved Key. Both paths use
`bind` in chat. The server distinguishes `REC-` and
`RST-` codes. Sending `bind RST-…` in the robot's group applies the reset.
The local `pending` entry in `connections` does not show the current server
status. Use its `next_command` to check whether the group already applied the reset.
When `enrollment-status` reports `chat_action: reset_applied` and the user asks
to continue, run its `next_command` to verify and save the current Key locally.
The verified group confirmation already authorized and applied the reset.
If a recovery code expired, request a new recovery code only
with the user's approval. An unavailable recovery result never starts a reset.
If a reset result expired or went missing, use fresh recovery proof to
reconcile it before considering another reset.

For a first binding on an older service, check its manifest for
`chat_credential_challenges`. If it is absent, use the compatibility command
`enroll` with the approved origin and selected storage scope. Pass `--authorized`
after the user approves requesting a code. When the user explicitly names a group,
`--group` can verify that name against the bot identified by chat confirmation.
Use the returned `enrollment_dir` for subsequent commands.

```sh
python3 "$LOCKBOT_CLI" enroll --origin https://resources.example.test --authorized
python3 "$LOCKBOT_CLI" enrollment-status --enrollment-dir "$enrollment_dir"
python3 "$LOCKBOT_CLI" finish-enrollment --enrollment-dir "$enrollment_dir"
```

Use the same confirmation sentence as for `key-challenge`.
The CLI stores the Key privately and
checks the service's binding and authenticated context before saving `connection.json`.
If it reports `bot_name_confirmation_required`, ask the user whether the verified bot
shown in the result is the intended one. After their explicit confirmation, add
`--confirm-bot-name "<verified bot name>"` to `finish-enrollment`. Reuse the same directory.
If the binding is still pending or expired, or the request outcome is unknown, report
that state and await the user. Never automatically request a second code.

Run `connections` to find saved records across sessions; it lists the origin, instance,
bot and record path without opening Key files. `pending_enrollments` lists unfinished
requests with their group, service and `enrollment_dir`. Use that directory with
`enrollment-status` to check current server state, even if the displayed code expiry
has passed. A binding confirmed before expiry can still be finished. For an explicit
enrollment directory outside the two catalogs, use `connections --directory <directory>`.
Verify the live identity with `context`
using the chosen record. By default, `init`, `key-challenge` and `recover` save under the
current user's private `~/.lockbot/connections/` catalog. For isolated tests set
`LOCKBOT_HOME` to an absolute directory. Default reads and writes use only its
`connections/` catalog while the variable is set. Without the override, `connections`
also discovers earlier user and project connection catalogs. Select
`--scope project` to save under `.lockbot/connections/`, or give an explicit
`--destination` for a separately managed path. `init` keeps an existing Key file at
its supplied path; newly enrolled and recovered Keys stay beside their connection
records. For an isolated project, save with `--scope project` and list only that
catalog using `connections --directory .lockbot/connections`. Add `.lockbot/` to the project’s Git ignore rules.

Start with an already-approved service URL and an existing private personal-key file. The
`init` command discovers the bot UUID through context; the user need not know a bot ID:

```sh
python3 "$LOCKBOT_CLI" init --origin <approved-origin> --key-file /private/resource.key --destination /private/connection.json
python3 "$LOCKBOT_CLI" --connection /private/connection.json context
python3 "$LOCKBOT_CLI" --connection /private/connection.json resources
python3 "$LOCKBOT_CLI" --connection /private/connection.json command --operation lock --node node-a --duration 30m --metadata-file /private/task.json
python3 "$LOCKBOT_CLI" --connection /private/connection.json command --operation renew --node node-a --duration 30m
python3 "$LOCKBOT_CLI" --connection /private/connection.json command --operation release --node node-a
```

Give each Agent installation a stable, recognizable label when you want to track its recent activity.
Pass `--agent-label "workstation-agent"` to `init`, or set `agent_label` in an existing private
connection JSON without changing its Key. The helper sends it as a UTF-8 percent-encoded
`X-Lockbot-Agent-Label` header on authenticated Lockbot requests. Use a short name with
at most 64 characters and keep secrets out of it. Requests without a label continue to work.

For card bots add explicit `--device 0` (repeat for more cards on that node). For the
same set of device IDs on every selected node, pass `--same-devices-on-all-nodes`. For
different sets, run separate commands per node. Private `task.json` accepts
only `task_category`, `agent_label`, `purpose`; choose the category from current context.
`init` saves metadata and a key-file reference, never rotates/rebinds or rewrites the key.
Keep connection/key files outside the installation. Credentials/grants are redacted in output.

## Identify the service before using a saved credential

This skill is for **Lockbot Resource API**, identified by
`service_id: lockbot.resource-api`, API version `1`, and personal `lbr_` keys.
A similarly named Lockbot skill, matching command URL, or remembered token is not proof
that it belongs here. Another Lockbot implementation can have a different authentication
protocol and permission scope. Check the advertised service identity and API version;
never transfer credentials or assumptions such as one token authorizing every bot.

Run `discover --origin <approved-service>` before setting up a connection. Use `init` for an existing Key or `key-challenge` with the user's approval when there is no valid local Key. In later sessions, run `connections` from the project and use `context` with the selected private connection. The CLI verifies the service, instance and Key's group. Ask for the address if it is missing and for the intended service if several match. Keep credentials scoped to the approved service and verified group.

## Keep credentials and resource assignments scoped

### Discover your existing bindings before enrolling again

Run `recover` with a verified seed connection to find other current bindings. On Feishu, recovery requires the same verified application and tenant. Equal user IDs from different applications do not establish the same identity.

When the user asks to move their existing access to another Agent, give the receiving
Agent **one existing active Key for the approved service via its private credential
store or a private file**. Use mode `0600` on POSIX, or a local Windows file whose ACL grants
access only to your account, SYSTEM, and local Administrators. Use `init` to verify it and create the seed connection
on that Agent, unless the connection already exists. Then use the fixed CLI:

```sh
python3 "$LOCKBOT_CLI" --connection /private/seed.json recover --destination /private/recovered-groups
```

Keep the seed connection. Use each returned `connection` for the corresponding group;
resource reads verify that Key's context before use. Report the group names and which
connections were saved or unavailable, without printing any Key. Use a new private
destination on each receiving Agent. One seed can recover other groups on the same
service and verified identity namespace when the server has a recovery copy. A group
whose Key was never stored for recovery needs its own existing copy. Keep each service separate.

Every group retains its own key. Recovery gives the user's Agents account-wide
credential discovery on this instance; resource routes still require the matching
bot's key. Never replace all group records with the key used to start recovery.

Legacy bindings remain valid. The first recovery request made with a legacy key
adds an encrypted recovery copy of that same key, without rotation. When the manifest
advertises `credential_recovery_health`, context reports only the current key's state:
`recoverable` (verified copy), `legacy_missing` (no encrypted copy), or `unreadable`
(copy cannot be verified, including an unavailable service encryption key). This is
recovery health, not credential validity; context never exposes another bot's state.

If another group has no recoverable copy, keep the existing Key and run `recover` with that group's verified connection if you have it. Check that the original Key, group and actor are unchanged.

Preserve an existing valid local key when a recovery entry has `credential: null` /
`credential_recoverable: false`: the historical copy is missing or unreadable, not
an instruction to rebind. Use another already authorized Agent/store holding that
bot's key to backfill it. Missing bot B material does not invalidate bot A or let A's
key operate on B. HTTP 503 `detail.code: credential_recovery_unavailable` means the
service could not create a copy; retain the valid key/configuration and report the
failure without retry loops, rotation or reset. A digest cannot reconstruct a key.

Only genuinely lost credentials or suspected disclosure/compromised authorization
justify considering revoke/re-enroll. First obtain explicit user confirmation of the
service, bot and revocation scope, explaining that every Agent sharing the affected
key loses access and needs private reconfiguration. In the affected group, the verified
human runs `bindings` to identify the old binding, then `unbind <short-id>` to revoke it.
Wait for confirmed revocation before requesting a fresh enrollment code. The human then
sends `bind <code>` in that same group. Sending only `bind <code>` while the old binding
is active leaves the old Key valid and the new code's Key unusable. An authorized
owner's revoke-all affects every personal binding on that bot. If revocation fails or
is denied, stop before enrollment. Existing locks and queues survive revocation.
Discuss and authorize any
resource changes separately. Never reset during a routine release, Skill update,
new Agent installation or migration, and never invent a key or silently rebind.

Older services may not advertise this capability: use known saved connections and
their context endpoints instead of probing bot IDs. Another service or protocol
requires its own verified connection, even when the displayed username matches.
If advertised recovery returns 404/405, keep the existing connections, report the
version mismatch and continue the saved-key flow; do not loop or rebind.
Keep browser login identity, each report's source, and each credential's scope distinct;
an aggregated report is not proof that one key authorizes every displayed group.

Manage several Lockbot connections with this skill. Keep a private connection record for
**approved origin + instance_id + bot UUID + verified user**, with a friendly name and a
reference to its securely stored key. Learn the bot UUID/user from `/api/agent/context`;
people do not need to manage these identifiers. Keep the last verified skill revision per
service instance. Never put keys in this skill, public notes, or task-purpose text.

For example, **service A / bot A / node1** and **service B / bot B / node1** are different
resources, even though both are called `node1`. Different bots on the same service are also
separate scopes. Select the intended connection first, load only its key, and verify its
context before use. If several connections could match the task, ask which bot/group the
user means; do not cycle through keys or try remembered keys against other servers.

Resource operations use **one user on one bot** per key. On recovery-capable services,
that key also authenticates the user for finding their other per-bot keys. The user's
Agents share each group key but track their own tasks separately:

- Associate each task allocation with **origin + instance_id + bot UUID + resource ID**,
  its Agent name/purpose, and the last verified holding/booking (`start_time` and
  `expires_at` when present). For card queues use the
  returned card `resource_id`, such as `node1:dev0`, in this private record; command syntax
  remains `lock node1 dev0`, not `lock node1:dev0`.
- Choose the credential by that bot scope, never by a bare machine/card name. Record which
  exact machines/cards this task acquired or booked; another Agent sharing the key may be
  working on other resources.
- A saved key/resource association is not proof of a current lease. Read `/resources` to
  confirm ownership and queue state before starting/resuming work and during long tasks.
  The same user may have released and reacquired a card through another Agent: if the
  saved lease/task no longer matches, pause and coordinate rather than assuming it is still
  this task's resource or releasing it just because the user identity matches.
- At task completion, release/cancel only this task's explicit resources/cards, then verify
  the result. Avoid bare `free`/`unlock`, which affects the user's other Agents too. Retain
  the shared key; do not unbind it merely because one task has finished.

### Resume from a saved connection

Use `connections` from the current project. Load the existing verified connection
for the intended service, group and actor. Use `init`, `key-challenge` or `recover` to create
connection records in the CLI's private catalog. Save each service and group's Key
separately. Keep connection and Key files out of Git and shared notes.

Keep task-allocation records separate from credentials. Recheck context and live
resources on resume. Ask for the service or group when multiple records match.

## Version checks and compatible updates

Use the bundled CLI for version and compatibility checks. When it reports `update_required`, you can still read resources and reconcile earlier writes. Before a new write, verify the complete ZIP on the same approved service, install it with the client's installer when authorized, then reload the Skill. Keep existing private connections and Keys. Do not rebind or repeat a command because the package changed. If the service is incompatible, tell the user and pause writes until a compatible package or service is available.
