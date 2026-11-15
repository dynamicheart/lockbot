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

- Credential form: `Bearer lbr_...`. Resource operations are scoped to a single `bot_id`;
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

### GET `/api/bots/{bot_id}/resources` — the authoritative read
There is **no push delivery** (no callback/stream/subscription). Poll this to learn state.
Returns: `health`, `resources` (holders with absolute `expires_at` + relative `remaining_seconds`),
`command_examples` (valid examples for this specific bot type), `captured_at`, `age_seconds` (>0 means served from a ≤5s cache — correct relative values by it or
prefer `expires_at`), `supported_operations`, your `actor`, and a per-caller `next_event_at` /
`next_event_kind`. Queue bots also include `queue_claim_window_seconds` and your queue position.

### POST `/api/bots/{bot_id}/commands` — one allowlisted mutation
Body: `{"command":"lock node1 2h","agent_label":"build-agent","purpose":"Develop feature X"}`.
`agent_label` (1–64 characters) and `purpose` (1–512 characters) are optional, single-line
private text; group notifications never publish these fields on servers advertising `usage_privacy`. Successful `lock` / `slock` / `book` also saves
these fields as that resource's task registration; no second API call is needed. Omit absent
fields. They never select your identity, authorize an operation, or give another rate-limit budget. Allowed words: `lock`, `slock`, `unlock`, `query`, `book`,
`alias`. Preemptive/administrative words (`take`, `kickout`, `kicklock`) and enrollment
(`bind`/`unbind`/`bindings`) are refused. The response `reply` is the bot's text, **not** a
structured outcome — always confirm effect by reading `/resources`.

### QUEUE_DEVICE: individual-card queues

`QUEUE_DEVICE` combines DEVICE card syntax with a separate FIFO queue for every card.
Read `bot_type`, `supported_operations` and `command_examples`; do not guess support from
another bot's remembered commands. For example:

- `lock node1 dev0 2h` acquires card 0; `book node1 dev0 30m` queues for that card.
- `free node1 dev0` releases/cancels only your card-0 work. Sibling cards and their queues remain.
- `lock node1 dev0,2 1h` or `dev0-3` selects several cards atomically. Omitting `dev...`
  selects every card on the listed nodes. Prefer exact cards so another Agent's work is preserved.
- Shared `slock` is unavailable. Personal keys still cannot `take`, `kickout` or `kicklock`.

Per-card state appears in `resources[].devices[]`. Each device has its own `booking_list`,
`claimable_by_you`, and `claim_command`. Follow that device's claim command after booking;
never treat a queued card as already acquired. Task metadata on `lock`/`book` is saved
per card, so the same person's different Agents can use different cards for different purposes.
For optional PUT/DELETE usage updates, use the device's exact `resource_id` (e.g. `node1:dev0`),
not its display label. Ordinary DEVICE bots retain their existing node-scoped declarations.

### QUEUE / QUEUE_DEVICE: booking is not task completion

For an immediate request such as “申请一台空闲整机”, inspect live occupancy and the
booking list. If the matching node or card is idle and its queue is empty, send
`lock` directly. If it is already claimable by you, use the advertised claim
command. `claimable_by_you: false` only means there is no booking you can claim;
an idle resource with an empty `booking_list` still supports direct `lock`.
Use `book` for an explicit reservation or when the user accepts waiting
for a busy or queued resource. A conflict on an immediate request calls for a
fresh read, not an automatic reservation.

If the user asks you to acquire/use a resource, do **not** finish after `book`. Booking only
reserves a queue position. Keep monitoring `/resources` yourself; do not wait for the human
or a group @mention to make the next API call. The `book` response's `next_step` repeats this.
When `resources[].claimable_by_you` (or a QUEUE_DEVICE device’s flag) is true, run
`command --operation claim --node <id>` within `queue_claim_window_seconds`. Include
one exact `--device <id>` for QUEUE_DEVICE. Omit `--duration` to retain the booked
duration. The helper rechecks eligibility, sends the exact `claim_command` and
verifies the new holding. When using direct HTTP, POST the verified `claim_command`
and read again to confirm the holding. Only then start the workload. If another holder remains, keep polling at a
cadence that suits the task. If monitoring is unavailable, report the queue position.

```text
For an authorized reservation, POST commands: book <resource> <duration> (include Agent name and purpose)
repeat at the task's polling cadence, following server Retry-After when returned:
    GET resources
    if the resource has my verified live holding: start/continue work; leave this loop
    if claimable_by_you: POST commands with claim_command; verify holding on the next read
    otherwise: continue polling using the next-event estimate as a hint
```

An existing task registered during booking is carried forward when you claim with `lock`
without new metadata. `free <resource>` cancels your booking or releases your lock.

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
| `command_not_allowed` | word is never reachable via a credential | do not retry |
| `command_not_supported` | this bot type has no such op | do not retry |
| `aliases_disabled` | deployment turned `alias` off | you are permitted; bot is not configured |
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
