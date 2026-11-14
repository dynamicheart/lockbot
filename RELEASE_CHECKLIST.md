# 3.3.0 release checklist

Completed 2026-11-14. See the [release record](docs/releases/3.3.0.md) for workflow links, digests, deployment evidence and acceptance limits.

- [x] Merge and accept dashboard, scoped recovery, monitoring, Skill origin and connection-source changes.
- [x] Review the candidate and pass all six CI checks; merged-commit CI also passed.
- [x] Obtain user approval for the concrete 3.3.0 publication and direct formal upgrade.
- [x] Publish the tag; verify Docker, PyPI, both telemetry bundles and saved-image workflows.
- [x] Record commit, image architecture, artifact ID and SHA-256; download and verify the image on the deployment server.
- [x] Preserve original port, data volume, JWT/encryption/metrics keys, bot configurations, locks/queues, routes and binding IDs/digests/authority.
- [x] Verify startup health, software version, database integrity, manifest capabilities, Skill/ZIP hashes and anonymous resource rejection.
- [x] Enable the native personal dashboard using explicit configuration; keep private integration adapters and browser sessions separate.
- [x] Accept existing integration feature coverage per the user's decision; do not claim repeated formal key-recovery/browser/group tests.
- [x] Record deployment evidence and synchronize #27/#57. Close the extra fresh formal-delivery gate as waived, not as a newly passed message test.

Migration #88 and evaluations #120/#121 remain explicitly deferred future work.
