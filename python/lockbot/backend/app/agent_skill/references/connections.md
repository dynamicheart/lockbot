# Bundled CLI

From the project directory, set `LOCKBOT_CLI` to the absolute path of the bundled
`scripts/lockbot_resource.py` beside the installed `SKILL.md`. `--help` lists commands, and
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
| Request a binding and finish after the user confirms | `enroll`, then `finish-enrollment` |
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

**MUST when the helper is available:** first confirm a Python 3.10+ interpreter and a
complete installed bundle, then use it for supported calls. `python3` below is an example;
`python` or Windows `py -3` may be candidates, but verify their actual version. If no suitable
runtime/installer/network is available, do not install dependencies without authorization or
claim the helper ran. Use an existing authorized HTTP tool with the same identity, rules and
readback checks, and report the helper limitation. Keep each connection's approved
service/instance/bot and private key-file reference; never put raw keys in argv or Skill text. Install/reload a changed bundle before mutation. Supply exact nodes/cards
and explicit durations for new acquisitions, bookings and renewals. Claim an existing
booking with `--operation claim` without a duration. For an existing holding, `renew`
uses the lock mode listed in `supported_operations` and adds the supplied duration
to the existing expiry. For release, use `release` without a duration. Verify readback
and report an uncertain write without repeating it.
This one-shot helper is **not** a workload runner or automatic renewal loop: continue to
follow the expiry/stop rules below.

For a new binding with no saved Key, supply the approved origin and the group named
by the user. Add the selected storage options to `enroll`. Pass `--authorized` only
once the user approves requesting the code. Use the returned `enrollment_dir` for
subsequent commands, shown below as `"$enrollment_dir"`.

```sh
python3 "$LOCKBOT_CLI" enroll --origin https://resources.example.test --group "Compute team" --authorized
python3 "$LOCKBOT_CLI" enrollment-status --enrollment-dir "$enrollment_dir"
python3 "$LOCKBOT_CLI" finish-enrollment --enrollment-dir "$enrollment_dir"
```

Show the returned `bind_command` prefixed with a mention of the bot, for the user to send
in the named group. Only the user confirms in chat. The CLI stores the Key privately and
checks the service's binding and authenticated context before saving `connection.json`.
If it reports `bot_name_confirmation_required`, ask the user whether the verified bot
shown in the result is the intended one. After their explicit confirmation, add
`--confirm-bot-name "<verified bot name>"` to `finish-enrollment`. Reuse the same directory.
If the binding is still pending or expired, or the request outcome is unknown, report
that state and await the user. Never automatically request a second code.

Run `connections` to find saved records across sessions; it lists the origin, instance,
bot and record path without opening Key files. Verify the live identity with `context`
using the chosen record. By default, `init`, `enroll` and `recover` save under the
current user's private `~/.config/lockbot/connections/` catalog. Select
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

1. Start with the server/skill URL explicitly supplied or approved by the user. Without
   a trusted server address, ask for that address once; never guess a host or bot ID.
2. **Without Authorization**, GET `/api/agent-skill` on that origin. Check `service_id`,
   `instance_id`, `api_version`, and `skill_id`. Do not follow a redirect to another origin.
3. For persistent setup, install/update `installation.download_url` as **lockbot-resource-agent**,
   beside other Lockbot skills. Keep their files and credentials intact. Verify the ZIP SHA-256
   and `skill_sha256` from this same trusted origin. Legacy download URLs stay supported.
4. Find saved credentials for the **trusted origin + instance_id**; use a known bot UUID
   to narrow matching records. A newly confirmed key needs no bot ID or UUID to call context;
   save its returned bot UUID alongside the key afterwards. Never use a single global
   `LOCKBOT_TOKEN`, generic "lockbot token" memory, or another platform's
   JWT. If an existing record has no service attribution, ask which service it belongs to;
   do not test it against different servers.
5. With a matching saved `lbr_` key, GET `/api/agent/context`. It supplies the bot ID, UUID,
   name/type, verified actor and relative operation URLs. Verify the returned instance ID
   against the saved record; use `links` on the same approved origin. Humans do not need
   to know or enter `bot_id`. This context works while the bot is stopped; when access is
   disabled it returns `agent_access_enabled: false`, so pause rather than bind again.

Do not send any key to a service just to identify the service. A public service identifier
prevents accidental protocol confusion; it does not authenticate a malicious host. The
user-approved origin (and HTTPS where available) remains the credential destination boundary.
The server cannot erase an Agent's earlier memories: keep the scope explicit in its private
configuration and use this namespaced skill for requests to this implementation.

## Keep credentials and resource assignments scoped

### Discover your existing bindings before enrolling again

After verifying the saved key with `/api/agent/context`, check the manifest for
`own_binding_directory` and follow `bindings_url` (also exposed as `links.bindings`
in context). `GET /api/agent/bindings` with that key lists current, unexpired bindings
for its exact verified platform/user **on this service instance only**. It returns
bot IDs/UUIDs/names, non-secret binding IDs, whether each is the current binding,
expiry and whether Agent access is enabled. Deleted/replaced bots and revoked or
expired bindings are excluded. A stopped bot can still have a valid binding.
Feishu recovery also requires the same verified application and tenant namespace;
missing namespace evidence stays bot-scoped. Equal ID strings from different
applications do not establish the same user.

When the user asks to move their existing access to another Agent, give the receiving
Agent **one existing active Key for the approved service via its private credential
store or a private mode-0600 file**. Use `init` to verify it and create the seed connection
on that Agent, unless the connection already exists. Then use the fixed CLI:

```sh
python3 "$LOCKBOT_CLI" --connection /private/seed.json recover --destination /private/recovered-groups
```

Keep the seed connection. Use each returned `connection` for the corresponding group;
resource reads verify that Key's context before use. Report the group names and which
connections were saved or unavailable, without printing any Key. Use a new private
destination on each receiving Agent. One seed can recover other groups on the same
service and verified identity namespace when the server has a recovery copy. A group
whose Key was never stored for recovery needs its own existing copy. Keep each service
separate. Direct API clients use the manifest's `binding_recovery_url`, also exposed as
`links.binding_recovery`, with the current Key and no request body.

Every group retains its own key. Recovery gives the user's Agents account-wide
credential discovery on this instance; resource routes still require the matching
bot's key. Never replace all group records with the key used to start recovery.

Legacy bindings remain valid. The first recovery request made with a legacy key
adds an encrypted recovery copy of that same key, without rotation. When the manifest
advertises `credential_recovery_health`, context reports only the current key's state:
`recoverable` (verified copy), `legacy_missing` (no encrypted copy), or `unreadable`
(copy cannot be verified, including an unavailable service encryption key). This is
recovery health, not credential validity; context never exposes another bot's state.

For each distinct saved key on the approved instance with a missing/unreadable copy:
POST recovery once using that exact key and no body; find the `current: true` entry;
privately compare its returned key with the original; then GET context again and
verify the same instance/bot/user and `recoverable` state. Keep the exact key value
and existing private-store reference. Reconcile each bot separately. If the field is
absent on an older service, keep the existing capability/context flow.

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

### Store the relationship; choose your own format

Reuse the Agent's existing private credential/configuration store. Do not require the
user to install a new memory system or maintain a particular JSON file. Each saved
connection needs a friendly name, approved origin, verified instance/bot/user identity,
and the reference to its securely stored key. Keep the last verified skill revision too.
No resource directory needs to be stored permanently: discover it from the live API.

If no suitable store exists, this optional flat JSON example is sufficient:

```json
{
  "schema_version": 1,
  "connections": [
    {
      "label": "Development group",
      "origin": "https://service-a.example.invalid",
      "instance_id": "<server.instance_id>",
      "bot_uuid": "<bot.uuid>",
      "bot_id": 1,
      "actor": {"platform": "Infoflow", "external_user_id": "<actor.external_user_id>"},
      "credential_ref": "<private-store reference for bot A>",
      "skill_revision": "<verified revision>"
    },
    {
      "label": "Testing group",
      "origin": "https://service-a.example.invalid",
      "instance_id": "<server.instance_id>",
      "bot_uuid": "<another bot.uuid>",
      "bot_id": 2,
      "actor": {"platform": "Infoflow", "external_user_id": "<actor.external_user_id>"},
      "credential_ref": "<private-store reference for bot B>",
      "skill_revision": "<verified revision>"
    }
  ]
}
```

`schema_version` versions this optional local format, not the server API or Skill.
Populate identities from confirmed context; the numeric bot ID and friendly name alone
are not enough to identify a credential scope. Both bots may contain `node1`: select the
bot connection first, then load its key. Another service gets a separate connection.
`credential_ref` is a reference, never a raw key; no particular credential-store product
or URI syntax is required. Keep configuration private and out of Git and shared notes.

Track task allocations separately as described above; update only the current task's
records after verified changes, coordinating concurrent writers to avoid lost updates.
On resume, recheck context and live resources. Do not move a key after a redirect, origin
change or instance replacement, and do not try other connections' keys after a failure.

## Version checks and compatible updates

The manifest separates `server_version`, stable `api_version`, and the opaque content-based
skill `version` (plus full `skill_sha256`). Compare skill revisions for **equality**, not as
ordered semantic versions. Check the manifest at task start and after reconnecting. Personal
API responses also include `X-Lockbot-Service-ID`, `X-Lockbot-API-Version`,
`X-Lockbot-Server-Version`, and `X-Lockbot-Skill-Version`. Check these on **every API response,
including errors**, not only context. Also send `X-Lockbot-Client-API-Version: 1`.
`X-Lockbot-Skill-Update` is `unknown`, `current` or `update_available`;
`X-Lockbot-API-Compatibility` is `unknown`, `current` or `incompatible`.
`X-Lockbot-Skill-URL` is the same-origin update path. A mismatch must cause a manifest check,
verified Skill download and reload before the next mutation; incompatible API versions must
pause mutations. Never replay a completed command merely because its response announces an
update. Propagate the actually loaded revisions and this check to delegated/background clients.

If the skill revision changes, re-fetch the manifest and this skill from the same trusted
origin before the next resource mutation. Use a cache-busting `?version=<revision>` or a
no-cache request, verify its SHA-256, and reload it without replacing another platform's
skill. An unknown API major version means stop mutations and request compatible instructions;
a server version change within API 1 does not itself require a new key or new binding.
Never renew/revoke credentials merely to update a skill.
For authenticated API calls, send `X-Lockbot-Client-Skill-Version: <loaded skill version>`.
Use the revision of the instructions actually loaded for this task, not a newer manifest
which you have not read. The downloaded Markdown and the installed package can have
different revisions. If you verified and read the full newer Markdown into this task,
send that revision on the next authenticated read, even while installation awaits approval.
A helper running from the old package still reports its own installed revision. Do not
override the helper's headers or claim that briefly loading Markdown updated the installed
package. Context, resource and command responses include `skill_update` in
the JSON body: `unknown` means no usable client version was reported; `update_available`
means it differs; `current` means the reported revision matches. These are equality checks,
not proof of successful local installation. Follow same-origin `manifest_url` / `skill_url`
and read the latest instructions before the next mutation when an update is needed. A
notice after a command does not mean that command failed: verify its state and never replay
it merely to update the Skill. Old clients may need the user to share the latest Skill URL
once because their installed copy contains no update rule or hook.

The API advertises updates; the Agent client performs the download and reload. A server
cannot automatically install a skill in a client that does not support it. Keep checking the
manifest at task start even if that client loaded an older installed copy.

Older 2.10.0 servers lack context discovery and the new identity fields. Their existing
bot-scoped APIs and keys still work with a previously approved bot configuration. If that
configuration is missing, explain that the server needs the discovery update, or ask for
its bot ID once as a legacy fallback. Never scan IDs or fall back to another platform's API.
