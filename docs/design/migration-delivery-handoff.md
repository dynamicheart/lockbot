# Migration delivery proof handoff

This extension belongs to the draft migration coordinator. It does not expose execution routes, enable migration, or migrate an existing bot.

A post-creation nonce delivered by the existing verified Feishu event transport proves that the reviewed application receives a real mention in the exact target group. It does not prove account ownership, publication status, or continued membership. The super administrator explicitly reviews publication evidence. No additional membership API permission is required.

Optional greetings and membership notices remain disabled by default. A known nonterminal migration can observe only its exact target's existing trusted membership events without sending notices or creating a group route. A fresh nonce works when no prior membership observation exists. A recorded removal invalidates readiness until an added observation is received.

## Epoch boundary

1. A source-epoch receipt binds the instance, operation, source digest/revision, target application fingerprint, tenant, group, current reviewer session, and reviewed publication evidence. It expires within two minutes.
2. The coordinator requires a fresh receipt before freezing and again before the identity switch. A five-minute metrics drain cannot extend the receipt lifetime; the administrator issues a new challenge while checkpointed.
3. The switch transaction validates that receipt and writes an immutable handoff reference together with the target route, revision increment, and source credential invalidation. A transaction failure publishes none of these changes.
4. The switched bot remains fenced. A distinct target-epoch challenge validates the exact new revision, target configuration digest, owned route, and durable handoff. Only a fresh signed response permits activation.
5. A source receipt never becomes a target receipt. The durable handoff preserves evidence across a crash; it is not itself fresh delivery authority. Recovery still requires the current administrator session and a valid target-epoch receipt.

The production validator performs no network requests and cannot execute resource commands. It uses the existing signature/official SDK ingress. The handoff callback must execute in the same database transaction as the identity switch; wiring only the validator without its handoff deliberately fails closed after switching.

## Schema and recovery

Two additive private tables store the proof epoch and atomic delivery handoff. Existing receipt rows without an epoch marker remain source-only and are not selected by the production execution validator. There are no changes to existing bot, account, allocation or credential fields. Nonces and message bodies are never persisted. Ordinary mid-migration archives remain refused; recovery requires the matching journal, source state, checkpoint, and lifecycle evidence.

The active holder/booking/offer identity migration and declaration re-enrollment remain separate requirements. This proof extension does not claim to implement those execution paths or make independent metrics authorities revocable.
