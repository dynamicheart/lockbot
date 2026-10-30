# Administrator migration workflow — scope correction

The current user decision is: “Clear all live runtime state during platform migration. Preserve the resource configuration, and have users enroll again on the destination platform.” This English translation is recorded in [issue #88](https://github.com/dynamicheart/lockbot/issues/88).

The earlier empty-state-first management draft is paused for revision. The requested workflow must preview the exact target and counts of live allocations, bookings/queues/offers, Agent grants/bindings and current usage declarations; a current super-administrator explicitly confirms that destructive reset. Target users enroll again. Active-state carryover and identity pairing are not required.

Resource inventory/configuration and immutable historical audit records are preserved. Clearing current runtime state does not authorize deleting history, replaying resource commands, silently reusing source credentials, running two independent allocators or rolling back later destination changes. Single-writer fencing, durable operation reconciliation, exact target/count checks and separate real acceptance remain required.

This draft interface and its older empty-state tests are not acceptance of the revised reset contract. Execution remains disabled until that implementation is reviewed and tested. No real migration has been performed by this UI work.
