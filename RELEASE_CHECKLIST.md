# 3.3.0 release checklist

Candidate scope and compatibility are documented in [release notes](docs/releases/3.3.0.md). The previous [3.2.0 release record](docs/releases/3.2.0.md) remains historical evidence.

- [x] Merge and accept the dashboard, scoped key recovery, Skill deployment origin and connection-source UI changes.
- [ ] Review the final release candidate and pass all six required CI checks on its current head.
- [ ] Obtain the user's publication approval for the concrete version and scope.
- [ ] Publish the tag and verify Docker, Python package, telemetry bundle and saved-image artifact workflows.
- [ ] Record commit, image architecture, artifact ID and SHA-256.
- [ ] Preserve the formal port, volume, keys/configuration, bot identities, live allocations, queues, routes and bindings during upgrade.
- [ ] Verify health/version, original-key context, manifest capabilities, resource permission boundaries and Skill/ZIP hashes.
- [ ] Verify the configured native dashboard and browser session flow using existing authorized credentials.
- [ ] Record actual deployment evidence and synchronize issue #27. Keep separate evidence gaps such as #57 visible.

Only the requested formal deployment and reviewed features enter this rollout. Deferred migration and network experiments retain their existing status.
