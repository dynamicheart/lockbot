# Agent-assisted Lockbot administration

Use this guide when the user asks you to deploy Lockbot or configure messaging platforms. It complements the repository's contribution rules; it does not authorize production changes, messages or external permissions by itself.

- [Deployment, upgrades and recovery](deployment/AGENTS.md)
- [Infoflow and Feishu onboarding](platforms/AGENTS.md)
- [Deployment example](../examples/lockbot-deployment/compose.yaml)

First identify the selected service, version, environment and user-authorized task. Keep local tests, integration services and formal deployments separate. Never infer permission to alter another installation from a similarly named bot or an existing Agent key.

Keep credentials in the user's approved secret store or a private local file outside Git. Never paste full `docker inspect` output, environment files, tokens, contact data or raw private task histories into a chat, issue or PR. Record sanitized validation outcomes and retain private evidence locally.

Agent assistance is practical for preparing configuration, running preflight, executing an authorized deployment, checking health and guiding platform setup. It cannot approve an enterprise application on behalf of a company administrator or guarantee third-party permissions/network access. Distinguish generated configuration, validated configuration, running service and real message acceptance.
