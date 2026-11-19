# Resource Agent helper and complete installation bundle

The resource Skill installation ZIP contains `SKILL.md`, on-demand `references/`, a standard-library Python client (`scripts/lockbot_cli.py`) and a local `installation.json` integrity/version stamp. Prefer the **same approved service's** `/api/agent-skill/lockbot-agent.zip`, discovered through its public manifest. Use the client's installer or verify and extract the complete package, then reload the Skill.

Existing API routes, personal keys and the legacy ZIP URL/root (`lockbot-agent-api`) remain supported. The public manifest points to the complete installation ZIP, which includes `SKILL.md`. Operator bundles remain separate and use operator credentials.

## Client runtime availability

Python is **not** a prerequisite for every Lockbot client or for downloading/installing the
Skill ZIP. Python 3.10+ is the runtime for this particular helper implementation. Confirm the
actual available interpreter first: `python3 --version`, `python --version`, or Windows
`py -3 --version` are candidates, not guarantees. Examples below use `python3` as the chosen
compatible interpreter. The helper refuses unsupported Python versions before making requests.

If there is no suitable interpreter, installer, private-file permission support or network
path, report the concrete missing capability. Do not install a runtime/dependencies without
authorization, fabricate a helper result or abandon a working key. The existing compatible
HTTP API remains available through an already authorized HTTP tool/credential store, following
the same service/instance/context/rules/readback and no-replay procedure. Installing Markdown
or downloading a ZIP does not prove the script can execute.

Native Windows uses `py -3` and local drives with persistent ACLs. Newly created private directories and
files grant access only to the current account, SYSTEM, and local Administrators. When importing a Key
or using an explicit destination, use a local private directory and verify the same ACL restrictions.
Use the normal project or user scope for automatic private directory creation. On macOS and Linux the
helper continues to use POSIX permissions.

## Private local resource labels

For private local annotations, use `resource label add REF LABEL`,
`resource label view REF`, `resource label list --label LABEL`,
`resource label remove REF LABEL`, or `resource label clear REF`.
These commands do not start a provider or read credentials, even through a
launcher configured with backend options. They use full canonical references
and a separate private metadata file; see the
[local label contract](../design/cli-private-resource-labels.md) for file location,
field discovery and bounded pagination. They never edit shared aliases.

## Explicit read-only resource providers

An explicitly selected local provider can supply resource lists and exact resource
views through the same CLI entry point:

```sh
python3 scripts/lockbot_cli.py --resource-provider /private/provider.json resource list --limit 20
python3 scripts/lockbot_cli.py --resource-provider /private/provider.json resource list --json
python3 scripts/lockbot_cli.py --resource-provider /private/provider.json resource view x-example/node-01
```

The registration is a private JSON file, outside Git, with exactly these keys:

```json
{
  "schema_version": 1,
  "command": ["/absolute/python3", "-I", "-S", "/absolute/provider.py"],
  "config_file": "/private/provider-config.json"
}
```

Choose trusted local code. The first command argument must be an absolute
executable path; subsequent arguments are passed literally without a shell. The
parent appends `--config CONFIG_FILE --provider-stdio`, sends one JSON request on
stdin, and never reads the provider's config or native connection catalog/Keys.
The provider is responsible for its own private configuration and credentials.
This selects one read source. The [mixed-source configuration](../design/cli-mixed-resource-sources.md)
can instead combine it with pinned native connections. Native `s-...` references, writes, device listing, native connections,
custom native directories, legacy mode, metrics and exact lease-time options
cannot be combined with `--resource-provider`.

The stdio request has `schema_version: 1`, an `action` of `list`, `view` or
`fields`, a `query` containing `nodes`, `model`, `fields` (or null), `limit` and
`mine`, plus `ref` and `cursor` (each a string or null). Its `budget` is
`{"max_http_requests":12,"timeout_seconds":60}`. Field names belong to the
provider; bare `--json` requests field discovery and must not open its config or
credentials. Discovery returns `json_fields` and `required_row_fields: ["ref"]`.

Successful reads return one JSON object with a `result` containing `rows`,
`has_more`, `next_cursor`, `scope_complete`, `http_requests`,
`capabilities: ["read_only"]` and `observed_only: true`. Each row needs a unique,
opaque `x-...` reference. Views return only the requested reference. Provider
cursors are preserved exactly: repeat the same provider query with `--cursor`.
The parent rejects oversized pages instead of trimming rows and skipping their
continuation. Controlled failures use exit code 2 and stdout JSON containing a
safe lowercase `error` code, `retry_mutation: false`, and optionally a finite,
nonnegative `retry_after_seconds`. An optional `http_requests` reports the actual
spent count, including zero for pre-I/O failures; omit it when unknown. Child stderr is discarded.

Mixed traversal extends the budget with `max_rows`, `max_output_bytes` and
`reference_namespace`. These runtime packing limits never change the original
query or cursor identity. A provider used in mixed mode must honor them. With a
namespace it emits `e1.NAMESPACE.BASE64URL(X_REF)` references before byte sizing;
the view request still carries its raw `x-...` locator. The parent validates the
decoded locator as well as the wrapper. It never drops rows to repair an oversized
provider response.

`inventory_scope` declares `full_inventory`, `own_only` or `unknown` coverage.
Optional `valid_for_seconds` and `coverage_valid_for_seconds` are durations from
zero to sixty seconds for row evidence and whole-query coverage respectively.
Missing durations are zero in mixed mode. They bound observed evidence age, not
continued holding or write authority. Known deadlines shorten them; missing
expiry does not invent a permanent lease or invalidate a fresh known booking.

An active list `--search TEXT` adds an optional canonical `search` string to the
query. Its literal word matching and input bounds follow the
[resource search contract](../design/cli-resource-contract-draft.md#search-names-aliases-and-hardware).
Apply it to full permitted observations before field selection or paging, not
to an already clipped response. A successful searched page includes
`search_mode: "literal_terms_v1"` and `search_applied` equal to the requested
canonical string, within the same output byte cap. Missing/incorrect confirmation
leaves `source_search_not_supported`; validated request cost is retained. Unknown
hardware that could match must prevent a complete negative result. Omit the
optional query key and acknowledgement fields when search is absent; ordinary
v1 requests, field discovery and cursor identity remain compatible. Exact views
must reject active search.

The parent enforces a 60-second process timeout and an 8-KiB final stdout limit.
It captures stdout in a private temporary file and reads only the bounded
response into memory. Trusted code can still grow that temporary file before
termination, and can access the local user's files or start other processes.
This execution interface is not a sandbox. Providers must omit credentials,
private paths and other people's records; the parent also rejects common secret
fields, token-shaped values and known registration/config paths in responses.

## Register accelerator inventory for whole machines

In a NODE or QUEUE bot's configuration, open **Node hardware → Register hardware**.
Copy the Agent prompt with the current node IDs and provide an approved machine
inventory. Paste the generated mapping, review the changes, and save:

```json
{
  "version": 1,
  "nodes": {
    "node-01": [
      {"model": "P900", "count": 2},
      {"model": "P800", "count": 6}
    ]
  }
}
```

The selected bot's owner or a super administrator can also preview the mapping with
`POST /api/bots/{id}/node-hardware/preview` and save it with
`PATCH /api/bots/{id}/node-hardware` using the Console session. Omitted nodes retain
their existing records. Set a node to an empty list to remove its record. Record
verified hardware only. An alias, IP address, or metrics label is not a hardware
record.

An Agent with a valid binding for the bot can read the registered model and count
in `resources[].accelerators`. For unregistered nodes, the Agent may keep confirmed
models and known counts in a private `connection.json.node-hardware.json` sidecar.
The [hardware reference](../../python/lockbot/backend/app/agent_skill/references/resource-operations.md#hardware-matching-node-addresses-and-aliases)
describes the scope and input format. The CLI shows these notes under
`resources[].local_hardware_note` when the node IDs match. NODE and QUEUE bots
allocate whole machines. Use the live occupancy and queue rules when deciding
whether the known hardware can be allocated now.

## Install and initialize

1. Fetch the approved service's `/api/agent-skill` **without credentials**. Verify `service_id=lockbot.resource-api`, `api_version=1`, the instance identity and namespaced installation descriptor. Follow only same-origin installation URLs and verify the exact ZIP `installation.sha256` before extraction.
2. Install and reload the complete ZIP with the Agent client's supported workflow. Keep every version-stamped file. If an older helper rejects the new archive layout, use the client's installer for the verified ZIP and then use its new bundled helper.
3. Keep the approved personal Key in a separate private file with POSIX mode `0600` or the Windows ACL described above. Keep the raw value out of command arguments, shell history, Skill text and shared memory. Reuse the existing Key when installing or updating the client.
4. Initialize a **new** private connection. The user need not supply a bot ID or UUID:

```sh
python3 scripts/lockbot_cli.py init --origin https://resources.example.test --key-file /private/resource.key --destination /private/connection.json
```

`init` first checks the credential-free manifest, then sends the Key only to that approved service's advertised context endpoint. It discovers the bound bot UUID and writes a private connection containing approved origin, instance ID, bot UUID and the Key file reference. Reuse the original Key file when upgrading. If context authentication fails, investigate the intended service and binding.

A connection has this shape:

```json
{
  "approved_origin": "https://resources.example.test",
  "instance_id": "discovered-instance-id",
  "bot_uuid": "discovered-bot-uuid",
  "key_file": "/private/resource.key",
  "approved_metrics_origin": "https://metrics.example.test"
}
```

The metrics origin is optional and must be separately approved; `init --metrics-origin <origin>` saves that approval. An Agent may maintain multiple such connections, each referencing its own scoped key. A key authorizes one verified user/bot; an Agent is not restricted to one connection. Keep these private files outside the installation so a Skill update cannot remove them.

### Codex project install

If Codex can download the verified ZIP but its workspace sandbox refuses writes to `.agents`,
finish the install in your own terminal. From the archive you already checked against
`installation.sha256`, extract `scripts/lockbot_cli.py` to a temporary location.
Use that verified helper from the project root:

```sh
python3 -I -S /path/to/verified/lockbot_cli.py install \
  --origin https://resources.example.test \
  --destination "$PWD/.agents/skills/lockbot-agent"
```

Replace the example origin with the same approved service. The installer fetches and
verifies the complete Skill without a Key and requires a new destination. Reload the
Skill list or start a fresh Codex session from the project to discover it. An existing
installation should use the client's update workflow.

### Connect without a usable local Key

```sh
python3 scripts/lockbot_cli.py key-challenge --origin https://resources.example.test --destination /private/compute-team --authorized
python3 scripts/lockbot_cli.py enrollment-status --enrollment-dir /private/compute-team
python3 scripts/lockbot_cli.py finish-enrollment --enrollment-dir /private/compute-team
```

Request the code after the user authorizes it. They send the returned bind command
to the bot in the group they want to connect. The CLI keeps the pending proof in
the private directory and saves the Key and verified connection after confirmation.
If there is no active Key, the same confirmation creates one. If an active Key
cannot be recovered, it stays valid. Use the same directory to check an outstanding
request after starting a new session. For first-time binding on older servers
without the challenge capability, use `enroll`.

### Move existing group access to another Agent

Privately copy one active Key into a file protected as described above on the receiving Agent. Run
`init` with that file to create the seed connection, then run:

```sh
python3 scripts/lockbot_cli.py --connection /private/seed.json recover --destination /private/recovered-groups
```

Keep using the original seed connection for its group. The CLI saves each available
additional group as a separate private connection and key. Subsequent commands verify
the chosen connection against authenticated context. Recovery is restricted to the
same verified deployment and identity namespace. Groups with no recovery copy are
reported without a connection. Existing files and keys are preserved. Transfer keys
through the receiving Agent's private credential storage.

## Fixed resource operations

```sh
python3 scripts/lockbot_cli.py --connection /private/connection.json context
python3 scripts/lockbot_cli.py --connection /private/connection.json resources
python3 scripts/lockbot_cli.py --connection /private/connection.json command --operation lock --node node-a --whole-node --duration 30m --metadata-file /private/task.json
python3 scripts/lockbot_cli.py --connection /private/connection.json command --operation claim --node node-a --whole-node
python3 scripts/lockbot_cli.py --connection /private/connection.json command --operation renew --node node-a --whole-node --duration 30m
python3 scripts/lockbot_cli.py --connection /private/connection.json command --operation release --node node-a --whole-node
```

Whole-node writes require `--whole-node`. DEVICE and QUEUE_DEVICE require explicit numeric `--device` arguments. Repeat `--node` for multiple exact nodes. If a requested card cannot be selected in the named group, ask the user before switching the group or scope. Supported operations are `lock`, `slock`, `book`, `claim`, deliberate `renew`, and `release`. For `lock`, `slock`, or `book`, omit `--duration` if the user did not specify a time. The helper checks the selected group's default before dispatch. If that default is missing or exceeds the applicable limit, specify a valid duration. Renewal always requires the amount of time to add. Claim uses the booked duration. An acquisition cannot silently renew an already held resource. Renewals require ownership, enabled renewal policy, a consistent shared/exclusive mode, and a genuinely later expiry in the readback. Booking an already owned/booked resource is rejected before POST. Resource IDs and supported operations come from verified context/resources; the helper does not guess a bot ID or send an arbitrary operation URL.

Resource mutations require verifiably fresh snapshots before dispatch and at readback.
The helper rejects a missing or invalid snapshot age and applies the server's five-second
resource-cache limit, including the time spent fetching the snapshot. A stale preflight
sends no command. A stale readback after dispatch leaves the result unconfirmed.

`task.json` is optional, uses the same private-file restrictions, and accepts only `task_category`, `agent_label`, `purpose`. The category must be one of the current context's public ID/label choices. A category error returns `allowed_categories` without echoing detailed notes, Agent names or Keys. Keep secrets out of private notes. Resource policy is enforced by the service.

Every dispatched write is followed by a resource readback. The server currently provides rendered `reply`, not a structured business outcome. Therefore the output explicitly says `verification: observed_state_only` and `command_outcome: not_structured_by_server`: it proves the requested observed state, not causality or guaranteed command success. HTTP 200 and a preexisting booking do not prove this command succeeded. Concurrent Agents sharing one identity still require coordination. A future additive structured command outcome is a separate API design, not inferred by parsing translated replies here.

On a queue bot, `claim` accepts exactly one eligible resource. Add one explicit
`--device` for a card queue and omit `--duration` to keep the original booked duration.
The helper checks a fresh, actor-scoped snapshot before using its exact claim command,
then verifies the holding. The caller continues to schedule polls and handle deadlines.

Timeouts, transport failures, 5xx, malformed write responses and unconfirmed readback never cause a write replay. The output reports uncertainty and `retry_mutation:false`; reconcile by reading resources first. This is a **one-shot client**, not a workload process runner, queue-wait daemon, autonomous renewal loop or guarantee that work stops at expiry. The Agent must still follow the Skill's lease-expiry, polling and workload-stop rules.

### Node login metadata

On POSIX, a fresh resource snapshot can populate a private `connection.json.node-access.json` file next to the selected `connection.json`. Each entry is keyed by node ID within the verified origin, service instance, bot, and holder scope. Cards share the parent node’s address. Entries contain the confirmed host, optional IPv4 address, SSH port, server lease expiration, and a conservative local `usable_until` Unix timestamp.

Entries past `usable_until` are expired even if the one-shot helper is idle and the file is still present. The helper clears previous entries when it starts an authenticated read or a resource change, and writes only from a current snapshot confirming an active hold. A failed authorization, released lease, or successful snapshot omitting the address therefore clears the corresponding entries. Always use fresh authorized resources before connecting. Keep this local file private, along with any other connection metadata and the separate Key file.

## Version and integrity checks

Before authenticated use, the helper checks the hashes of its installed Skill, script and reference files and content-derived revision against its local stamp. Each invocation identifies the service without a key, pins the instance and verifies the returned bot UUID. It reads current context, follows same-origin advertised resource/command links and sends the client Skill/API version headers. Version changes from the manifest, context, response headers or body notices mark the bundle outdated. Mutations stop until the client installs and reloads the current complete bundle; read-only reconciliation remains possible when an older intact bundle is still compatible. A modified/incomplete bundle fails local integrity checks.

The published `version` covers the rendered Skill, the helper and all reference files. `skill_sha256` continues to hash **only the served Markdown**. Both the legacy and namespaced ZIPs retain their original roots/Markdown paths, and include the helper, references and stamp; each ZIP's SHA-256 covers its exact bytes. Deployment API addresses are rendered from `PLATFORM_URL`, not untrusted forwarded headers.

The packaged helper can fetch and verify a new installation into a **new** destination, without any key:

```sh
python3 scripts/lockbot_cli.py install --origin https://resources.example.test --destination /private/new-skill
```

It refuses an existing destination, unexpected/duplicate ZIP entries, symlinks, oversized expanded content and mismatched hashes. The complete installation becomes visible at the destination in one step. If a local write fails before publication, fix the storage or permissions and retry the same destination. After a successful install, reload the Skill using the client's workflow. Use the client's update workflow for an existing Skill and preserve external connection/key files.

## Shared node aliases

```sh
python3 scripts/lockbot_cli.py --connection /private/connection.json alias --node node-a
python3 scripts/lockbot_cli.py --connection /private/connection.json alias --node node-a --set "Training"
python3 scripts/lockbot_cli.py --connection /private/connection.json alias --node node-a --clear
```

The alias is a public label on the parent node. Use at most 15 characters.
Readback uses `aliases_observed_at` and reports an empty string for an unset alias.
Older responses without structured alias readback cannot confirm a value.
Writes require the current bot's alias editing rule and are checked against a
new resource response. Repeating the currently observed value skips the write.
Review the command response separately for persistence information. A matching
observation alone cannot attribute the change or guarantee it survives restart.

## Best-effort metrics

```sh
python3 scripts/lockbot_cli.py --connection /private/connection.json metrics --node node-a
```

This implementation supports only the existing `type: prometheus`, `profile: lockbot-v1` source configuration and its fixed metric family. The generic/custom profile and arbitrary PromQL are not implemented. Select at most 64 explicit configured node names per query; the helper translates only the context's node mapping and refuses unmapped targets. It reads compute/memory/CPU/memory/cache/filesystem/collector-timestamp/success series, not arbitrary process or command information. The data source still enforces its own ACL.

A short-lived source uses the context's `metrics:read` grant. An auth failure refreshes context and retries once; the original personal key is never sent to the source. Independent authorization uses a separate private `metrics_key_file`; personal `lbr_` and OP `lbk_` keys are explicitly refused. Source origin must match the separate approval, including scheme/port. No redirects or fallback through the Lockbot server are attempted.

The result is marked `best_effort` and `verification: query_response_only`. The raw vector is under `data`. The `utilization` array contains one entry per requested Lockbot node and reports CPU and cards found in the response. `sample_age_seconds` derives from the relevant collector's sample timestamp. `percent` is populated only for `ok` readings, including valid zero. Missing, failed, stale and invalid readings return `null`. A card omitted by the source does not appear in the array. Compare against known inventory before drawing conclusions about coverage. Resource ownership and workload progress require independent evidence.

Unreachable, unsupported, unauthorized, oversized or malformed sources fail explicitly. The helper queries existing samples and does not trigger hardware collection or resource mutations.

## Bounded transport and validation evidence

The helper uses Python's standard library only: no shell subprocess, MPI, extra package, GUI dependency or automatic job process. One invocation has a 60-second check budget, up to 12 HTTP attempts, a 10-second network timeout per request, a 16-KiB request body limit and a 1-MiB response limit. Reads may retry a single explicit `429 Retry-After` of at most 30 seconds within the remaining budget. When a 429 reaches the caller, the CLI reports a valid server delay as `retry_after_seconds`. Read the current state after waiting that long before making another mutation decision. Slow network reads remain bounded by the per-request timeout and remaining-budget checks. Writes never retry automatically.

Catalog reads reuse a validated anonymous discovery descriptor only within the current invocation, for the same approved origin, pinned instance and client profile. Each connection validates that descriptor against its own configuration before opening its Key, then reads and validates its own protected context and resources. Contexts, grants and Keys are never shared through discovery reuse, and no discovery cache is written to disk. A scope error clears descriptors for that origin before later Keys. A same-origin server replacement can still occur between discovery and a protected request; context checks detect contradictory responses but do not eliminate this time-of-check/time-of-use window. After discovery, the helper reserves the minimum protected read requests for the scope; a scope that cannot fit remains at the continuation cursor without opening its Key. The 12-attempt and 60-second invocation budgets stay unchanged.

Tests use real bounded fake HTTP servers for redirects, identity/version checks, metadata validation, uncertainty, readback and grant refresh. Separate real API/core fixtures exercise initialization, acquisition, renewal and release across NODE, DEVICE, QUEUE and QUEUE_DEVICE. Distribution regressions check rendered addresses, hashes, revision headers, old URLs and actual wheel inclusion. These are not claims of deployment or a live hardware-source query: authenticated real-service/data-source acceptance is a separate gate.

## One installed package, independently approved services

The reference-enabled archive declares `package_format=canonical-origin-v2` and a
`package_version` over the canonical instructions, helper and references. Only the exact
builder-generated deployment locator is restored to its template marker. The current
helper can also verify older `canonical-origin-v1` archives. The installed rendered files, ZIP hashes and original
`version` checks remain intact. A pasted deployment Markdown still contains its real
service address.

An identical package from another approved deployment can serve separately stored
connections without repeatedly replacing the installation. The helper compares package
revisions only when both the verified local stamp and remote manifest declare this
format. A changed Skill, helper or reference still blocks the next mutation until an authorized
update; an old stamp/server or unrecognized locator stays on conservative rendered
revision comparison. It never substitutes the server revision for the installed one.

Existing `X-Lockbot-Skill-Version`, `X-Lockbot-Skill-Update` and JSON `skill_update.status`
continue to compare deployment-rendered bytes. New `X-Lockbot-Package-Version`,
`X-Lockbot-Package-Update` and `skill_update.package_status` describe portable instruction
compatibility; clients report `X-Lockbot-Client-Package-Version` as well as their unchanged
rendered revision. These are advisory compatibility signals, not proof of installation
or authorization. API-version compatibility is still checked separately.

Equal package revisions never make service identities or credentials interchangeable.
Each connection still requires its own approved origin, verified instance, bot, actor and
private key reference. Old APIs, download names and keys remain unchanged. This does not
add dynamic reporter commands, cross-origin key discovery or automatic client updates.
