# Enforcing privacy at the Agent handoff boundary

Installing a Skill does not prove that an external Agent follows it. The optional standard-library reference module [`lockbot.agent_client.handoff`](../../python/lockbot/agent_client/handoff.py) demonstrates an executable final payload gate for a cooperating client or orchestrator. It is not an HTTP SDK, a server-side sandbox, an installer, or a guarantee about arbitrary external Agents. Nothing in this module sends a command, resolves a credential reference, uploads logs or opens an issue.

## Trusted parent and child responsibilities

1. The parent selects the approved service origin and a **private-store reference**, loads that connection's key outside this module, and authenticates `/api/agent/context` on that exact origin. Disable redirects that might forward credentials. Do not scan bot IDs or try other stored keys after a failure.
2. Call `make_handoff(origin, context, credential_ref, policy)`. Store the original envelope in parent-owned state outside the child's writable workspace. Send the child only a JSON-serialized copy. Do not include a raw key, private objective, full prompt or log in the envelope.
3. The child proposes a command and optional fields. It cannot grant itself a broader privacy mode. The parent authorizes the actual command separately and obtains fresh authenticated context using the same key reference and origin.
4. Immediately before compiling the HTTP body, call `final_command_payload(original, child_copy, ...)`. It compares the policy, service, instance, bot UUID/numeric ID, resource actor and key reference. A mismatch raises `GateError`: stop, do not try other keys, retry or replay a previous command. Pass **only the returned fresh dictionary** to the caller's HTTP encoder; never merge child fields afterward.
5. The caller handles timeouts, ambiguous responses, Skill/API version notices and user authorization. Updating the Skill must not automatically replay an operation that may already have executed. This helper has no queue, persistence or retry loop.

The JSON handoff is versioned independently of API or Skill versions:

```json
{
  "schema_version": 1,
  "scope": {
    "origin": "https://resources.example.invalid",
    "service_id": "lockbot.resource-api",
    "instance_id": "instance-a",
    "bot_uuid": "bot-a",
    "bot_id": 7,
    "actor": {"platform": "Infoflow", "external_user_id": "user-a"},
    "credential_ref": "keyring:resource-group-a"
  },
  "privacy_policy": {"version": 1, "mode": "command_only"}
}
```

These are neutral placeholders, not deployment identifiers. `credential_ref` is an opaque local reference, not a key or a transport URL. The helper rejects known bearer-key prefixes as an additional guard, but cannot determine whether every arbitrary string is a secret: the parent must supply an actual store reference. Context is authenticated by the caller's transport, not by trusting self-declared JSON. HTTP on an internal network does not provide TLS identity assurance; use the network controls required by the deployment.

## Privacy modes

| Version 1 mode | Final body |
| --- | --- |
| `command_only` (default) | Only the validated `command`; all optional child fields are discarded. |
| `public_category` | `command` and optionally one `task_category` ID from the current authenticated context's `usage_privacy.public_task_categories`. Unknown IDs raise `GateError`; display labels or free summaries are not IDs. |
| `private_details` | `command` plus optional bounded `purpose`/`agent_label`, only when the parent explicitly selects this mode and the server declares both fields private through `usage_privacy.private_fields` and `legacy_purpose_visibility: private`. |

Unsupported envelope/policy versions and modes are rejected. A server without the needed privacy capability causes a **command-only downgrade**, including old servers. Other identity fields, authorization labels and arbitrary extra keys never enter the returned body. Private mode does not implicitly enable a public category. The helper does not guess a category from internal work or publish a shortened objective. Private details remain subject to the server's reader permissions and logging; they are not end-to-end encrypted notes.

`command` is validated as bounded single-line text, not parsed for user authorization or secrets. In particular, `alias` commands can themselves contain public text. The parent must authorize commands and validate their arguments against the user's intent and bot rules; a field allowlist alone cannot sanitize every possible command. This distinction also prevents treating a successful privacy gate as permission to perform a resource mutation.

Run the offline example:

```sh
PYTHONPATH=python python examples/agent_handoff.py
# {"command": "lock node-a 30m"}
```

It performs no network access or resource operation. Its deliberately supplied child `purpose` and `agent_label` are stripped.

## Identify this task's lease without public labels

Keep task allocations separately in parent-private state, associated with the trusted handoff. Capture the actual resource ID, authenticated actor, holder `start_time` and `expires_at` from successful commands/fresh resource snapshots. The current resource API exposes these timing fields; do not fabricate a `holding_id` if it is not available. If a server exposes a holding ID, save it and require the fresh holder to match it as well.

```python
from lockbot.agent_client.handoff import lease_matches

# Obtain `context` using this same origin/key reference, and `holder` from a
# fresh authenticated resource snapshot for this exact resource (card included).
saved = {
    "resource_id": "node-a:dev0",
    "actor": context["actor"],
    "start_time": "2026-01-01T10:00:00+00:00"
}
ours = lease_matches(
    trusted, origin=origin, authenticated_context=context,
    credential_ref=credential_ref, resource_id="node-a:dev0",
    saved=saved, holder=holder, now="2026-01-01T10:30:00+00:00"
)
```

`lease_matches` checks scope, resource, real actor, exact start instant, live expiry and the optional holding ID. A matching Agent label is irrelevant. Another task's later lease for the same user/resource fails the start-time check. If evidence is missing or uncertain, stop rather than issuing a broad `free`/`unlock`. Shared keys cannot isolate the same user's Agents, so coordinate their private allocation records.

This is a client preflight, **not** an atomic server compare-and-release: state can change after the query. Do not claim it solves concurrent mutations; coordinate before releasing a task's resources and re-read when uncertain. Removing optional fields must not trigger a new lock or renewal. If the user authorizes clearing a current usage declaration, first match this task's lease and declaration using the authenticated owner view, then delete only that resource's own declaration via the existing usage-declarations endpoint. It is a metadata cleanup, not `free`; never clear every declaration or another task's current declaration to remove a name. The current delete API has no lease precondition, so concurrent ownership/declaration changes require coordination or a stop. Previously posted group messages cannot be recalled by this helper.

## Feedback goes to the verified service's manifest

Use `feedback_url(trusted, manifest_origin=origin, verified_manifest=manifest)` after retrieving the manifest from the approved origin and verifying its service/instance identity. It returns that manifest's HTTPS `issues_url`, rather than a hardcoded product-name guess or a different platform's stored feedback route. It does not authorize a submission, open a browser or upload anything. Ask for explicit issue-submission authorization where not already given, and sanitize a proposed report before submitting; do not attach raw prompts, keys, private URLs, task details or logs by default.

## Adoption and remaining acceptance

Unit tests simulate parent → JSON handoff → child proposal → final payload, including malformed policy, cross-bot/key scope, old servers, private capability checks, unknown categories, extra-field stripping, lease reuse and manifest mismatch. Seven additional in-process HTTP tests authenticate real API context, pass the gate result directly to the JSON encoder, execute the first lock through the actual API/core, and inspect own/other-user resource reads for NODE and QUEUE_DEVICE in all three privacy modes. Changed authenticated actors stop before sending a mutation. These use fictional resources and a test HTTP transport; they are not a live external-Agent integration or production deployment. These are repository reference capabilities, not evidence that an arbitrary external Agent has installed or adopted the gate. External clients must integrate it at their actual final serialization boundary, protect parent-owned policy state, and perform their own first-operation/end-to-end checks. Existing server-side privacy projection remains necessary even when clients cooperate. Issue #24 remains open for that adoption and real client acceptance.
