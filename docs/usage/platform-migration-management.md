# Administrator migration workflow

The migration-management interface is restricted to current super-administrators. It is a separate, explicitly enabled execution workflow; the existing read-only preflight does not authorize a cutover. When the service does not advertise management support or execution is disabled, the page shows that boundary and performs no write probe.

The initial workflow accepts only an empty resource state. Review the target application and group, then explicitly create a durable plan. The page displays the operation ID, complete target digest, identity revision, exact durable phase and server-selected next step. Keep the operation ID with the review record. Reopening the page reads the latest operation, including recovery after a platform switch; it does not resume it automatically.

For a verification challenge, supply the opaque 32-character hexadecimal reference of the actual application-publication review record and confirm the displayed target digest. This is an administrator's explicit attestation, not a claim that the page has independently checked provider approval. The page never generates a fictitious review record. Copy the short-lived `migration-check` command and send it with a real mention of the reviewed application in the target group. The page itself does not send messages or resource commands. Refresh the operation to check whether the server accepted the signed group evidence.

Advance and abort each require an explicit confirmation tied to the displayed operation/phase. The server remains authoritative for freshness, proof, metrics-authorization expiry and the legal next transition. Abort is offered only before identity switching; it is not a post-switch rollback. An uncertain request outcome requires an explicit status refresh before another write. The client does not retry, replay commands or start a background transition/poll loop.

Changing bot identity, account or target invalidates pending responses and displayed challenge material. Only allowlisted status fields are retained in the interface; credentials, detailed Agent notes and raw server errors are not rendered. Closing the page cannot undo a server operation already committed. Use the durable operation status to reconcile it rather than creating another plan blindly.

This interface does not establish live migration acceptance, support active allocations or make a disabled deployment ready for production. Real migration, receiver handoff, restart/recovery and formal rollout require the separately reviewed deployment gates.
