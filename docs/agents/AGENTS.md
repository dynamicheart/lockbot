# Agent-assisted Lockbot administration

Use this guide for deployment, messaging platforms, utilization datasources, and OP API work. Follow the matching guide before running tools. The user's request determines the authorized scope.

- [Deployment, upgrades and recovery](deployment/AGENTS.md)
- [Infoflow and Feishu onboarding](platforms/AGENTS.md)
- [Maintenance](#maintenance)
- [Deployment example](../examples/lockbot-deployment/compose.yaml)

First identify the selected service, version, environment and user-authorized task. Keep local tests, integration services and formal deployments separate. Never infer permission to alter another installation from a similarly named bot or an existing Agent key.

Keep credentials in the user's approved secret store or a private local file outside Git. Never paste full `docker inspect` output, environment files, tokens, contact data or raw private task histories into a chat, issue or PR. Record sanitized validation outcomes and retain private evidence locally.

When an operator supplies a secret-bearing file for deployment preflight, pass its path to the [deployment helper](deployment/AGENTS.md#prepare-configuration-without-deploying). Do not open the file in a reading tool or print it in a terminal. If the helper cannot run, report the blocker and leave its checks unverified.

Agent assistance is practical for preparing configuration, running preflight, executing an authorized deployment, checking health and guiding platform setup. It cannot approve an enterprise application on behalf of a company administrator or guarantee third-party permissions/network access. Distinguish generated configuration, validated configuration, running service and real message acceptance.

## Maintenance

- [Datasource setup and validation](../usage/datasource-setup.md) covers connecting an existing source, deploying a utilization collector and checking browser access. It includes the local configuration generator.
- [OP monitoring and authorized release](../usage/operator-api.md#monitor) covers current holders. Use the Monitor section for current state and the separate history endpoints for past activity. Check `/api/public-docs/op` on the approved deployment for its current contract.

Use the datasource's own read-only access for metrics and the target bot's OP key for operator calls.

## Bot rule discovery

- Running Agents find the lookup command and how to match a resource to its bot in the [v2 Skill](../../python/lockbot/backend/app/agent_skill/v2.md). The service advertises the Skill download and OpenAPI URL in `GET /api/agent/v2/manifest`.
- On the target service, open `/api/public-docs/agent-v2` and find `GET /api/agent/v2/bindings` for the field guide and response example. Its [operation documentation](../../python/lockbot/backend/app/resource_api/v2/docs.py) and [rule models](../../python/lockbot/backend/app/resource_api/rules.py) are maintained here. Tools can read the same response schema at `/api/agent/v2/openapi.json`.
