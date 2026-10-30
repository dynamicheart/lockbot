# 3.2.0 release checklist

Preparation only: `main` declares 3.2.0; formal production is still 3.1.0. Merged code, accepted integration behavior, publication and formal deployment are separate states. Track delivery in [#27](https://github.com/dynamicheart/lockbot/issues/27).

## 1. Freeze the release scope

- Release the existing merged features and fixes, including PR #119 chart hit-target geometry; review the final diff and exact commit.
- Platform migration [#88](https://github.com/dynamicheart/lockbot/issues/88) is deferred to a later release. Its unmerged reset/backend/UI branches are preserved and excluded. Architecture evaluation #120 and CLI/SDK onboarding #121 are future ideas, not release gates.

## 2. Retain the bounded integration evidence

- [#54 installed sequence](https://github.com/dynamicheart/lockbot/issues/54#issuecomment-6527870715): six fictional commands, eight committed events over nine frozen public OP pages; consumer/CLI agree. Starting coverage is unknown and grant totals remain null. Resources ended idle; temporary OP authority was revoked and its key file deleted. This does not prove complete history, resource-hours or productivity (#21).
- [#63 membership](https://github.com/dynamicheart/lockbot/issues/63#issuecomment-6529107006): real removal/re-addition in an already-bound disposable group passed; warning cleared, route/resources remained intact and optional switches were restored off. An unbound-group greeting remains unverified; it is not an additional release blocker.
- Retain accepted reaction/panel, detailed-note authorization/audit and registry compatibility evidence; do not reinterpret test-enterprise acceptance as corporate permission approval.

## 3. Validate the exact release head

- Complete review, required six CI checks and package/frontend builds on the final head. Keep API version 1 and default-off optional features.
- Retain scope/privacy, original-key compatibility and no-command-replay regression checks; do not add unrelated acceptance experiments.

## 4. Publish and verify artifacts

- Under the existing release authorization, tag/publish 3.2.0 and verify image/bundle workflows, downloaded artifacts and checksums. Record exact commit and artifact identities.
- Preparation documentation is not evidence that publication or deployment happened.

## 5. Upgrade and verify the formal service

- Preserve its port, volume, original keys/configuration, bot identities, bindings and active locks/queues; use the established upgrade procedure.
- Verify actual health/version, credential registry, public/detailed-note boundaries and metrics after upgrade. Record preservation/results in #27/#57/#80 and the roadmap; keep broader #21/#54 analysis and unverified #63 greeting scope explicit.
