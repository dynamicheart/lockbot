# Administrator migration execution flow

This is a deployment-gated implementation stacked on the draft migration coordinator. Do not enable it on a production deployment before the complete stack's fault, permission, expiry and confirmation acceptance passes. It supports empty-state Infoflow to Feishu cutover; mapped active allocations remain a separate required delivery.

Set `LOCKBOT_ENABLE_PLATFORM_MIGRATION=1` only after acceptance. The default is disabled. The read-only `/api/bots/{id}/migration-management` returns capability and the latest operation, allowing a page reload to discover a switched/frozen operation. It does not issue a plan, proof, credential or audit event. Only a current super administrator can access any management endpoint. Neither ordinary OP keys nor delegated detailed-note keys grant migration authority. These management routes are absent from anonymous OpenAPI.

## API

- `POST /api/bots/{id}/migration-plan`: `{target_application_id, target_chat_id, operation_id}`. The operation ID is a client-created 32-character hexadecimal idempotency key. Reusing it for another target is refused. Nonempty holds, queues and offers are refused without changing them.
- `GET /api/bot-migrations/{operation_id}`: safe status, exact source/target revision, target digest, `next_action`, `can_resume`, and `can_abort`. No source/app secret or user task text is returned.
- `POST /api/bot-migrations/{operation_id}/target-challenge`: `{reviewed_target_digest, publication_evidence_ref, publication_reviewed:true}`. The evidence reference is an administrator-reviewed opaque 32-character hexadecimal reference to a real publication/approval review. It is not a provider publication-status API result. The response includes the one-use command, expiry and projected status. The proof's source/target epoch is derived server-side.
- `POST /api/bot-migrations/{operation_id}/resume`: `{expected_phase}`. A changed phase is refused under the execution lock. After committing the identity switch, `phase:switched / next_action:target_challenge` is an ordinary durable outcome. The bot stays frozen until the new signed target proof is consumed.
- `POST /api/bot-migrations/{operation_id}/abort`: `{expected_phase}`. Available only before switching; it reopens the current proven source state, never an old checkpoint. A switched bot cannot be silently reversed.

Actions are explicit; clients must not blindly replay writes after a network timeout. Inspect the operation ID to learn which durable phase committed. Status exposes a wait deadline rather than blocking an HTTP request for the metrics drain. A current request's original JWT version is checked again after blocking work; it never adopts a newer ORM token version.

## Independent metrics expiry review

No configured data source requires no metrics policy or wait. For a configured `short_lived` source, the first executable flow conservatively waits **315 seconds plus a deployment-reviewed clock bound after freezing**. It does not add a grant issuance ledger and does not claim to know which users still hold tokens. Independent/nonconforming sources are refused until their authority policy has a separate reviewed implementation; no metrics traffic is proxied through Lockbot.

The existing gateway enforces a 300-second maximum token lifetime and 15-second expiry leeway. A deployment operator must review that actual gateway configuration and a bounded issuer/verifier clock difference. Store a private regular file (absolute path, owner root/current service user, mode `0600`) and set `LOCKBOT_MIGRATION_METRICS_POLICY_FILE` to it. Example:

```json
{
  "version": 1,
  "issuer": "example-instance-id",
  "audience": "https://metrics.example.test",
  "profile": "lockbot-gateway-v1",
  "maximum_token_ttl_seconds": 300,
  "expiry_leeway_seconds": 15,
  "verify_expiration": true,
  "maximum_clock_skew_seconds": 2,
  "review_reference": "gateway-clock-review-record",
  "review_valid_until": 1800000000
}
```

Replace the example issuer/audience with the existing deployment identity and canonical signer audience, and the review deadline with a Unix timestamp no more than 24 hours ahead. No signing secret goes in this policy or in HTTP input. The deployment signer registry remains the existing authority for signing keys. This file is explicit operator evidence, not automatic clock synchronization or remote gateway attestation. If that evidence is unavailable, expired, or incompatible, execution fails closed.

The management plan stores its rounded-up wait bound in an additive private table. A renewed review may tighten that bound but cannot enlarge it after the wait deadline was computed. Publication and delivery proofs are not extended across the wait. Before switching, obtain a new two-minute source-epoch proof if needed; after switching, obtain a distinct target-epoch proof.
