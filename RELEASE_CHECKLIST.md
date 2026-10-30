# Release checklist

Release goal: deploy the current Lockbot features, reviewed fixes and the explicitly confirmed live-state-reset migration. SDK/CLI registration, active-state carryover and identity pairing are not this release's requirements. Source, integration acceptance, publication and formal deployment are separate facts.

## 1. Freeze the final scope

- [ ] Freeze the reviewed candidate on protected `main`; document its exact commit and remaining gates in [#27](https://github.com/dynamicheart/lockbot/issues/27).
- [ ] [#88](https://github.com/dynamicheart/lockbot/issues/88) uses the latest decision: clear this bot's live holders, queues/offers, Agent authorizations and current declarations; retain resources/configuration and immutable historical records. Target users enroll again. The old natural-drain/pairing draft is not a release requirement.

## 2. Accept the reset migration on fictional resources

- [ ] Show exact target, authoritative clear counts and digest; require explicit super-administrator destructive confirmation. Changed counts/identity/target must fail closed.
- [ ] Verify reset, target enrollment and single-writer recovery without source-command replay, restored cleared leases or deletion of history. No real resource migration is implied by mock/fixture tests. Record the accepted scope under #88.

## 3. Verify regression and permission boundaries

- [ ] Keep Infoflow/Feishu messaging, current keys/configuration and ordinary resource operations compatible. Check registry preservation under [#57](https://github.com/dynamicheart/lockbot/issues/57) and scoped audit disclosure under [#80](https://github.com/dynamicheart/lockbot/issues/80).
- [ ] Distinguish the bounded OP request report and isolated V2 HTTP evidence from installed-service full-sequence acceptance: [#21](https://github.com/dynamicheart/lockbot/issues/21), [#54](https://github.com/dynamicheart/lockbot/issues/54). Do not claim productive work, measured utilization or complete history from request counts.
- [ ] Record the remaining real membership-event boundary in [#63](https://github.com/dynamicheart/lockbot/issues/63); keep optional unaccepted features off. Do not silently expand enterprise permissions or close unfinished issues.

## 4. Build and publish the reviewed candidate

- [ ] Required lint, frontend checks, Python matrix and build pass on the final combined head; review the complete diff and use the protected merge process.
- [ ] Check package/frontend/Skill/API versions, then perform only authorized tag/release/image/artifact actions. Download and verify the actual artifact checksum; do not reuse older green checks or claim a build from a source merge.

## 5. Upgrade and verify the formal service

- [ ] Preserve the existing port, data volume, encryption/JWT configuration, credentials and all existing locks/queues during the software upgrade. Only a separately confirmed migration may clear its selected bot's live state. Retain a consistent backup and recovery evidence.
- [ ] Verify installed version, health, Skill content/version hints, permission projections, resource state and configured metrics. Record actual deployment and remaining acceptance gates in #27/#57; a successful test-service deployment is not the formal upgrade.
