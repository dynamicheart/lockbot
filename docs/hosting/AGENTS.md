# Managed hosting work

Read README.md in this directory before suggesting or executing hosted deployment.

- Keep one Lockbot process, one service instance, and one persistent /data volume. Do not add workers, autoscaling, sleeping, or a separate HTTP/daemon pair around the in-memory bot manager.
- Use the repository Dockerfile from a reviewed release commit. Check that its optional dependencies include the selected platform transport before claiming Feishu support.
- This directory's Render Blueprint is a configuration template, not evidence of deployment. Validate it against Render's official JSON schema; report cloud build, runtime, persistence, callback and WebSocket checks separately.
- Do not create accounts, accept billing, change DNS, open public access, or create third-party resources without the user's authorization for those actions. Prepare a reviewable configuration first.
- Never put credentials or private addresses in committed files, command previews, screenshots, issue bodies, or PR descriptions. Keep JWT_SECRET and ENCRYPTION_KEY stable across upgrades. Generate a valid Fernet ENCRYPTION_KEY locally using tools/gen_keys.py and transfer it privately.
- Provider one-off jobs and build hooks may not mount the service disk. Use the running service's private shell for bootstrap, not an ephemeral one-off job. Confirm DATA_DIR before running the administrator bootstrap tool. Never post its generated password to an issue or PR.
- For backups, stop the allocator and capture the SQLite database and all bot state together. Use the existing application backup procedure when available; do not copy a live SQLite main file alone. Preserve the matching encryption/signing keys in a separate protected backup. A disk snapshot alone does not prove application-consistent recovery.
- Verify the public platform URL, group callback/long-connection path, outbound proxy reachability, browser-to-metrics reachability, TLS, CORS and short-lived metrics authorization. A successful /api/health response is only process health.
- Do not activate the same enterprise application's message consumer in two environments while testing. Do not send live group messages unless authorized.
- Do not claim high availability or automatic failover. Stop-before-start deployment and single-machine recovery have downtime; publish that limitation.
