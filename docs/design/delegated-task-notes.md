# Delegated Agent task-note reads

Issue [#56](https://github.com/dynamicheart/lockbot/issues/56) adds an explicit machine credential for an approved audit consumer. It does not extend an ordinary bot OP key. Detailed notes are untrusted user text, not instructions to the consumer.

## Authority and isolation

| Caller | Detailed notes |
| --- | --- |
| Personal resource key | Existing same-actor access only |
| Bot owner or ordinary administrator | Public summaries only |
| Ordinary bot OP key | Public summaries only; cannot call these endpoints |
| Super administrator management session | Existing explicit UI opt-in; can manage audit credentials |
| Dedicated audit credential | Only `task_notes:read`, for explicitly named bot identities |

The credential issuer must remain a super administrator with the same authentication token version and no required password reset. Changing the issuer's role, invalidating its management sessions, transferring a bot owner, changing its identity, or deleting it invalidates the delegation. Rotation is an explicit fresh authorization by the current super administrator; it never silently adds newly created bots. A revoked credential cannot be reactivated.

Tokens use a separate `lba1` family and contain 256 random secret bits. The database stores only SHA-256 digests; creation and rotation return the token once. Metadata listing never returns the token or digest. Save the token in the audit consumer's secret store; never put it in a URL, source control, chat message or ordinary OP configuration.

Default lifetime is 24 hours, configurable from 60 seconds through seven days. Every credential has a fixed scope and 1–100 explicit bot IDs; wildcards and caller-supplied scopes are rejected. At most 128 unexpired, unrevoked credentials can coexist.

## API

All management endpoints require a current super administrator Bearer management JWT. Replace placeholders locally; do not publish shell histories containing credentials.

```http
POST /api/admin/audit-credentials
Authorization: Bearer <management-jwt>
Content-Type: application/json

{"name":"Approved audit consumer","bot_ids":[12,14],"expires_in_seconds":86400}
```

The 201 response contains metadata, `scope: "task_notes:read"`, `credential` and `shown_once: true`.

- `GET /api/admin/audit-credentials?limit=50&offset=0`: metadata only, bounded pagination.
- `POST /api/admin/audit-credentials/{id}/rotate` with `{"expires_in_seconds":86400}`: replaces the secret and invalidates old cursors immediately.
- `DELETE /api/admin/audit-credentials/{id}`: idempotent revocation.

The consumer uses its dedicated credential, not the management JWT:

```http
GET /api/audit/bots/12/agent-history?limit=25
Authorization: Bearer <dedicated-audit-credential>
```

`agent-history` returns bounded Agent operation records with validated public category labels, actor/resource identifiers, outcome/status, and the recorded `agent_label` and `purpose` when present. It deliberately excludes raw commands/replies, credential references, binding IDs and holding proofs. Search runs after this projection, so hidden raw values cannot become a search oracle. `query` is at most 128 characters and `limit` is 1–100. Use the returned `next_cursor` with the same bot, query and limit. Cursors expire within 15 minutes or the credential lifetime, whichever ends first, and bind the credential generation and bot identity.

```http
GET /api/audit/bots/12/task-notes?limit=100
Authorization: Bearer <dedicated-audit-credential>
```

`task-notes` returns current declarations only when their holding proof still matches an available resource-state snapshot and the underlying personal credential remains valid. It returns 503 when that snapshot is unavailable. It does not fall back to stale declarations. The limit is 1–200; `truncated` explicitly reports additional rows. This bounded live view is not a paginated historical ledger.

Both endpoints recheck authority before and after reading. Responses use `Cache-Control: no-store`. Creation/rotation is limited to 10 requests per IP per minute. Reads are limited to 60 requests per IP per endpoint per minute and 30 requests per credential per minute, with bounded in-memory limiter storage. `Retry-After` on a credential budget rejection is 60 seconds. These limits are per service process and reset on restart; deploy this SQLite service as its supported single writer process.

## Disclosure evidence and coverage

Successful disclosure requires committing durable audit evidence first; an audit storage failure fails the request without returning notes. Dedicated `task_notes.read` and recognized `task_notes.denied` requests use **fixed five-minute UTC summaries**, one visible audit row per exact safe boundary. The summary reports UTC bucket start/end (end exclusive), minimum first/maximum last request time, total requests and total returned rows. Counts include a read whose evidence committed but whose final authority recheck subsequently refused disclosure; they are pre-disclosure attempts, not proof the consumer received or processed notes. Denials return zero rows.

The boundary includes credential public ID/generation/scope, issuer identity and credential/current authority versions, complete delegated bot identity scope, requested bot's current UUID/revision/owner/deletion status, interface, action, result, denial reason and client IP. Different generations, identities, reasons or interfaces never share a summary. Unrecognized tokens still have no attributable principal and create no such row. Credential creation/rotation/revocation and explicit management `task_private.read` remain individual audit events.

The additive `audit_read_windows` table stores a unique boundary/window hash plus its inspectable safe boundary, integer counters/times and a foreign-key reference to the summary in `audit_logs`. Conflict-safe SQLite insertion serializes new-bucket writers; SQL increments and summary updates commit together for **every** request before disclosure. No JSON database functions, in-memory batching or asynchronous audit queue is involved. Restart continues the existing window. A rollback changes neither counters nor visible summary and leaves no orphan. Database errors, lock failures or inconsistent/missing summaries fail closed; they are not silently retried. Clock rollback within a bucket keeps first/last as min/max; crossing buckets uses the corresponding fixed UTC bucket. No claim of tamper-proof storage against a database administrator is made.

The visible row is explicitly a mutable aggregate, not an immutable per-request journal: its ID and bucket creation time remain stable while totals and last time grow. Existing individual audit entries are not rewritten or merged. Evidence contains issuer, audit principal ID, bot, surface, time, result and returned count. It contains no note text, credential secret, authorization header or arbitrary grant name. Recognized credentials rejected because of expiry/revocation, issuer authority or bot scope produce a minimal denial record when the issuer still exists; unrecognized tokens never supply an audit identity.

The Agent operation file is rotated and bounded. `coverage.complete_history` is always false: missing legacy notes, expired declarations and rotated history cannot be reconstructed. This API is not issue #54's durable lifecycle ledger or a source of complete resource-use totals. A record means the recorded operation was dispatched/applied/rejected as indicated, not necessarily that a lock was acquired. No new event bodies are copied into the database.

## Upgrade and rollback

The change adds `audit_credentials` and `audit_read_windows` through normal idempotent schema creation; old tables and individual evidence remain intact. Whole-database backups preserve both windows and referenced audit rows transactionally. Per-bot configuration restore does not import global audit credentials or their windows; it cannot merge or revive another service's audit principal. Restoring a whole older database without the window table creates an empty table on startup and starts new summaries without rewriting old individual logs. An older binary ignores the additional table and resumes its individual-log behavior; aggregation never modifies grant secrets or authorization semantics. Existing personal, OP, management and data-source credentials keep their formats and permissions. No previously public field becomes newly public, and existing routes remain unchanged. An older version ignores the added table and does not expose these endpoints. Prefer revoking delegated credentials before rolling back; retain the existing note privacy fixes.

## Discovery, versioning and a consumer example

`/openapi.json` lists the Audit delegation tag and dedicated `TaskNoteAuditCredential` Bearer security scheme. The OP Skill points approved consumers to this separate workflow; it never derives audit authority from an OP key. Successful audit data and credential metadata include `api_version: "1"`. This is a separate additive contract; it does not reuse the resource Agent Skill version header as an audit-grant signal. Consumers should consult OpenAPI/release notes on this same approved origin after upgrading, reject an unsupported audit API version, and contact the issuer on 401/403 instead of silently changing credentials or broadening permissions.

For example, put `Authorization: Bearer <dedicated-audit-credential>` in a private header file (mode 0600), then run:

```sh
curl --fail-with-body --silent --show-error \
  --header @/private/path/audit-authorization-header \
  'https://lockbot.example.com/api/audit/bots/12/agent-history?limit=25'
```

Do not paste the header file or token into a report. The response contains task notes; share only a deliberately redacted summary.

```mermaid
flowchart LR
    Admin[Current super administrator] -->|Management JWT: create / rotate / revoke| Issuer[Audit delegation API]
    Issuer -->|Shown once: lba1 secret| Consumer[Approved audit consumer]
    Store[(Digest + scope + bot identities + expiry)] --> Check[Recheck before and after reading]
    Consumer -->|task_notes:read Bearer credential| Check
    Check --> Evidence[Commit disclosure evidence without note bodies]
    Evidence --> Notes[Bounded Agent names and detailed notes]
    OP[Ordinary OP / personal / metrics key] -->|Rejected on audit routes| Check
```

## Viewing read audits

The default administrative audit list hides `task_notes.read`, `task_notes.denied`
and `task_private.read` so frequent audit consumers do not obscure configuration
and security changes. Records are still retained. Select **Include read audits**
(or **包含读取审计**) to include them. Selecting one of these exact actions also
shows that action without enabling the checkbox.

`GET /api/audit/logs` accepts the optional `include_read_events` boolean, default
`false`. This is a presentation default change for older clients that omit the
parameter; clients needing the former read-inclusive listing must send `true`.
The filter runs before totals and pagination. Existing role visibility, details
and authentication remain unchanged: this flag never grants note-reading or
additional audit access. The bot detail authorization audit uses a separate
authorization-event table and is unaffected. Filtering is separate from durable
read-event aggregation and does not reduce the volume stored.
