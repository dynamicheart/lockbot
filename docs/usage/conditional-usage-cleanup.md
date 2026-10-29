# Conditional task declaration cleanup

Use this flow only after the user authorizes removing this task's declaration. Metadata cleanup does not release resources, renew a lease, recall group messages, or isolate Agents sharing the same personal key.

1. Authenticate `/api/agent/context` on the approved service using this connection's personal key. Check `usage_privacy.conditional_cleanup`; do not assume an old server supports it.
2. Read the current resource and your own declaration. Match the task's saved actor, exact resource ID and lease evidence. A declaration revision alone does not establish ownership.
3. Save the declaration's private `revision`, a quoted opaque comparison value. Send that exact value as `If-Match` on `DELETE /api/bots/{bot_id}/usage-declarations/{resource_id}` using the same connection and personal key.
4. HTTP 200 with `{"deleted":true}` means this metadata row was removed. The resource lock and queue remain unchanged. HTTP 412 means the declaration is missing, expired, replaced, or no longer belongs to that live lease: stop and inspect current state. Never fall back to an unconditional delete.

The server authenticates and rechecks the actor, serializes with the existing authority guard, compares the current declaration and live lease, and includes the observed row contents/version in the SQL delete predicate. Another process replacing the row cannot turn a stale conditional request into deletion of its replacement. A revision cannot authorize another actor or bot. It is omitted from public resource/declaration views; ordinary OP and bot-administrator projections do not gain access.

The revision is derived from the current declaration, including its private content and lease evidence, and should be treated as an opaque private value. It is neither a raw holding proof nor a secret credential. Even equal update timestamps do not make changed contents the same revision. No database migration or resource state format change is needed.

For compatibility, requests that omit `If-Match` keep the earlier idempotent delete contract. They do not receive the conditional concurrency guarantee. Unsupported preconditions, wildcards and weak validators are rejected rather than treated as unconditional. This endpoint accepts the one exact revision returned by Lockbot, not a list of generic HTTP entity tags.

This protects metadata cleanup only. The resource `free`/`unlock` commands remain separate operations and do not gain atomic compare-and-release semantics from this feature. Shared-key tasks must still coordinate their own allocation records, and uncertain resource mutations must not be replayed automatically.
