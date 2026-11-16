# Direct HTTP fallback

**Read-only HTTP fallback when the packaged CLI cannot run.** On the approved origin,
GET `/api/agent-skill` without credentials and verify the saved service and instance.
Then GET its `context_url` (`/api/agent/context`) with
`Authorization: Bearer <personal-key>`, using the matching private `lbr_` Key.
Send `X-Lockbot-Client-Skill-Version` with the version of the instructions you actually
loaded, from `installation.json` for the installed copy. Verify the returned identity
and GET the same-origin `links.resources` with the same authentication. Use those
paths and header names as given. A Key proves its bot without a user-supplied bot ID.
A pending update still allows you to read resources and verify the outcome of an earlier
command. If asked to prepare an update without installing, verify the complete ZIP against the manifest's archive digest, then verify `SKILL.md` against
`skill_sha256`, load it for the current task, and give the user the manifest's ZIP URL for
confirmation. A subsequent authenticated read reports the newly loaded revision. Keep the
installed package and Key in place until the user authorizes a change. Never replay a finished
command to check its outcome.

## Authentication

- Credential form: `Bearer lbr_...`. The Key determines the resource group;
  same-user binding discovery/recovery is instance-wide. It acts as the IM user who
  enrolled it. You cannot choose or spoof the identity — it is fixed at enrollment.
- **Enrollment is chat-only.** A human runs `bind <code>` in the bot's IM group to confirm a
  pending credential. `bind` / `unbind` / `bindings` are refused over HTTP by design.
  On Feishu, mention the enterprise bot in the explicitly configured resource group;
  direct messages do not enroll a resource credential. Preserve the exact `actor.platform`
  from context in connection memory; matching display names across platforms are not the same identity.
- One verified user has one active key per bot; their own agents share it. Other users and bots have separate keys. Do not repeatedly enroll to get a key per agent.
- Agent access is enabled by default for new bots; an explicit disabled setting is retained. The owner can change it and save the configuration. Disabling access pauses it; a valid key can work again after re-enabling. `unbind` permanently revokes the key.
- A credential lives until revoked (unless this deployment configures an expiry). It is revoked by: chat `unbind`, an owner revoke, bot delete,
  or a restore that overwrites the bot (identity discontinuity). After revocation, calls return
  `401 credential_invalid`.

## Endpoints

### GET `/api/agent/resources` — the authoritative read
There is **no push delivery** (no callback/stream/subscription). Poll this to learn state.
Returns: `health`, `resources` (holders with absolute `expires_at` + relative `remaining_seconds`),
`command_examples` (valid examples for this specific bot type), `captured_at`, `age_seconds` (>0 means served from a ≤5s cache — correct relative values by it or
prefer `expires_at`), `supported_operations`, your `actor`, and a per-caller `next_event_at` /
`next_event_kind`. Queue bots also include `queue_claim_window_seconds` and your queue position.

## Error codes

Read `detail.code` when `detail` is an object; otherwise read the top-level `code`.
Published 401 `detail` strings and HTTP statuses are preserved for older clients; new
auth responses add `code` and `context_url` without changing those fields. On 2.10.0,
a string-only 401 has no machine code: do not interpret it as permission to scan bot IDs.

A 401 tells you that the server rejected the credential on that request. It does not
identify revocation, expiry, or a change to the saved Key. On an approved origin, first
check that the request sent the complete original Key from the selected connection,
without truncation, masking, or surrounding whitespace. Let the helper or a trusted
HTTP tool load the private credential directly when it can. If a write may have
succeeded, correct any request-format error and use a bounded read of context or
resources to reconcile. Never replay the write, rotate the Key, or enroll again
solely because a read returned 401. If the complete credential is still rejected,
report that observation and ask the user before any credential change.

| code | meaning | action |
|---|---|---|
| `bot_not_running` / `instance_retiring` | transient | retry later (honour `Retry-After`) |
| `admission_full` | the bot's concurrency slot is full | back off, `Retry-After` |
| `rate_limited` | you exceeded your per-credential budget | back off, `Retry-After` |
| `credential_missing` | no Bearer key | load this origin/instance's own key |
| `credential_invalid` | unknown, revoked, expired or foreign key, or an incorrect value in this request | verify the saved origin, instance and exact credential; reconcile prior writes with a read |
| `wrong_bot` | valid key, wrong existing bot URL (legacy HTTP 401) | GET `/api/agent/context` and use its operation links; do not bind again |
| `may_have_executed` | admitted but outcome unknown | read `/resources`; do not repeat the command |

## Discoverability

`GET /api/agent-skill` returns this skill's version, download URLs and ZIP SHA-256.
`GET /api/agent-skill/lockbot-resource-agent.zip` downloads the complete Skill and
its bundled helper. Read `SKILL.md` inside that verified archive.
These endpoints need no API key and contain no user-specific configuration or credentials.
Resolve relative URLs against the approved origin and use the manifest's namespaced `installation` bundle.


The machine-readable contract is served at `/api/public-openapi.json` (browsable at
`/api/public-docs`); it is a self-contained closure covering only the public routes.
