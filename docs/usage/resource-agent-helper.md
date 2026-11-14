# Resource Agent helper and complete installation bundle

The resource Skill installation ZIP now contains the Markdown, a standard-library Python client (`scripts/lockbot_resource.py`) and a local `installation.json` integrity/version stamp. Prefer the **same approved service's** `/api/agent-skill/lockbot-resource-agent.zip`, discovered through its public manifest. A URL alone does not install anything in every Agent client; use the client's installer or verify/extract the package, then reload the Skill. Copying only Markdown does not install the helper.

Existing API routes, personal keys, legacy ZIP URL/root (`lockbot-agent-api`) and Markdown URL stay supported. Scripts are an additional recommended client, not a mandatory server gate for older clients. Operator bundles remain separate and do not gain this personal-key client.

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

Local validation covers the available macOS Python runtime and fake HTTP/real API fixtures.
Windows `py -3`, Windows ACL equivalence to POSIX mode0600, environments without Python, missing
installer, unavailable networking and other Agent runtimes remain explicit acceptance cases.
Private files are refused when this helper cannot verify its permission constraint; this
change does not pretend to provide cross-platform ACL handling or Go/Node alternatives.

## Install and initialize

1. Fetch the approved service's `/api/agent-skill` **without credentials**. Verify `service_id=lockbot.resource-api`, `api_version=1`, the instance identity and namespaced installation descriptor. Follow only same-origin installation URLs and verify the exact ZIP `installation.sha256` before extraction.
2. Install/reload the complete ZIP with the Agent client's supported workflow. `SKILL.md`, `scripts/lockbot_resource.py` and `installation.json` must all be present. A copied script or copied Markdown alone is incomplete.
3. Keep the already approved, existing personal key in a separate private file, mode `0600`. Do not put its raw value in command arguments, shell history, Skill text or shared memory. Do not rebind or rotate a working key merely to install or update this client.
4. Initialize a **new** private connection. The user need not supply a bot ID or UUID:

```sh
python3 scripts/lockbot_resource.py init --origin https://resources.example.test --key-file /private/resource.key --destination /private/connection.json
```

`init` first checks the credential-free manifest, then sends the key only to that approved service's advertised context endpoint. It discovers the bound bot UUID and writes a mode-0600 connection containing approved origin, instance ID, bot UUID and the key-file reference. It does not change the key, enroll another bot, recover unrelated credentials or overwrite an existing connection. Reuse the original key file when upgrading. If context authentication fails, investigate the intended service and binding; do not try the key against arbitrary hosts.

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

## Fixed resource operations

```sh
python3 scripts/lockbot_resource.py --connection /private/connection.json context
python3 scripts/lockbot_resource.py --connection /private/connection.json resources
python3 scripts/lockbot_resource.py --connection /private/connection.json command --operation lock --node node-a --duration 30m --metadata-file /private/task.json
python3 scripts/lockbot_resource.py --connection /private/connection.json command --operation renew --node node-a --duration 30m
python3 scripts/lockbot_resource.py --connection /private/connection.json command --operation release --node node-a
```

Repeat `--node` for multiple explicit nodes. DEVICE and QUEUE_DEVICE require explicit numeric `--device` arguments; omission cannot silently select all cards. Supported operations are `lock`, `slock`, `book`, deliberate `renew`, and `release`. Duration is explicit for acquisition, booking and renewal. An acquisition cannot silently renew an already held resource. Renewals require ownership, enabled renewal policy, a consistent shared/exclusive mode, and a genuinely later expiry in the readback. Booking an already owned/booked resource is rejected before POST. Resource IDs and supported operations come from verified context/resources; the helper does not guess a bot ID or send an arbitrary operation URL.

`task.json` is optional, private mode `0600`, and accepts only `task_category`, `agent_label`, `purpose`. The category must be one of the current context's public ID/label choices. A category error returns `allowed_categories` without echoing detailed notes, Agent names or keys. Do not put secrets in private notes either. All allocation policy enforcement remains server-authoritative.

Every dispatched write is followed by a resource readback. The server currently provides rendered `reply`, not a structured business outcome. Therefore the output explicitly says `verification: observed_state_only` and `command_outcome: not_structured_by_server`: it proves the requested observed state, not causality or guaranteed command success. HTTP 200 and a preexisting booking do not prove this command succeeded. Concurrent Agents sharing one identity still require coordination. A future additive structured command outcome is a separate API design, not inferred by parsing translated replies here.

Timeouts, transport failures, 5xx, malformed write responses and unconfirmed readback never cause a write replay. The output reports uncertainty and `retry_mutation:false`; reconcile by reading resources first. This is a **one-shot client**, not a workload process runner, queue-wait daemon, autonomous renewal loop or guarantee that work stops at expiry. The Agent must still follow the Skill's lease-expiry, polling and workload-stop rules.

## Version and integrity checks

Before authenticated use, the helper checks its installed Markdown/script hashes and content-derived revision against its local stamp. Each invocation identifies the service without a key, pins the instance and verifies the returned bot UUID. It reads current context, follows same-origin advertised resource/command links and sends the client Skill/API version headers. Version changes from the manifest, context, response headers or body notices mark the bundle outdated. Mutations stop until the client installs and reloads the current complete bundle; read-only reconciliation remains possible when an older intact bundle is still compatible. A modified/incomplete bundle fails local integrity checks.

The published `version` covers both rendered Markdown and script bytes. `skill_sha256` continues to hash **only the served Markdown**. Both the legacy and namespaced ZIPs retain their original roots/Markdown paths, now with real script and stamp files; each ZIP's SHA-256 covers its exact bytes. Deployment API addresses are rendered from `PLATFORM_URL`, not untrusted forwarded headers.

The packaged helper can fetch and verify a new installation into a **new** destination, without any key:

```sh
python3 scripts/lockbot_resource.py install --origin https://resources.example.test --destination /private/new-skill
```

It refuses an existing destination, unexpected/duplicate ZIP entries, symlinks, oversized expanded content and mismatched hashes. It does not claim to have registered/reloaded the package in the Agent client or created a connection. Use the client's update workflow for an existing Skill and preserve external connection/key files.

## Best-effort metrics

```sh
python3 scripts/lockbot_resource.py --connection /private/connection.json metrics --node node-a
```

This implementation supports only the existing `type: prometheus`, `profile: lockbot-v1` source configuration and its fixed metric family. The generic/custom profile and arbitrary PromQL are not implemented. Select at most 64 explicit configured node names per query; the helper translates only the context's node mapping and refuses unmapped targets. It reads compute/memory/CPU/memory/cache/filesystem/collector-timestamp/success series, not arbitrary process or command information. The data source still enforces its own ACL.

A short-lived source uses the context's `metrics:read` grant. An auth failure refreshes context and retries once; the original personal key is never sent to the source. Independent authorization uses a separate private `metrics_key_file`; personal `lbr_` and OP `lbk_` keys are explicitly refused. Source origin must match the separate approval, including scheme/port. No redirects or fallback through the Lockbot server are attempted.

The returned vector is marked `best_effort` and `verification: query_response_only`. A successful query is not proof of fresh/valid hardware data, resource ownership or workload progress; inspect the source's real sample timestamp/collector health and follow existing freshness rules. Unreachable, unsupported, unauthorized, oversized or malformed sources fail explicitly and do not trigger resource mutations or hardware collection. This client does not bypass CORS/network/IT policy or run SMI.

## Bounded transport and validation evidence

The helper uses Python's standard library only: no shell subprocess, MPI, extra package, GUI dependency or automatic job process. One invocation has a 60-second check budget, up to 12 HTTP attempts, a 10-second network timeout per request, a 16-KiB request body limit and a 1-MiB response limit. Reads may retry a single explicit `429 Retry-After` of at most 30 seconds within the remaining budget; missing/unreasonable hints fail for later reconciliation. Slow network reads remain bounded by the per-request timeout and remaining-budget checks. Writes never retry automatically.

Tests use real bounded fake HTTP servers for redirects, identity/version checks, metadata validation, uncertainty, readback and grant refresh. Separate real API/core fixtures exercise initialization, acquisition, renewal and release across NODE, DEVICE, QUEUE and QUEUE_DEVICE. Distribution regressions check rendered addresses, hashes, revision headers, old URLs and actual wheel inclusion. These are not claims of deployment or a live hardware-source query: authenticated real-service/data-source acceptance is a separate gate.
