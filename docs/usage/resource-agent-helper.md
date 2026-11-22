# Install and use the LockBot Skill

The LockBot service publishes `SKILL.md` and its SHA-256 in the Agent API v2
manifest at `/api/agent/v2/manifest`. Set `PLATFORM_URL` at deployment to the
HTTP or HTTPS address that Agents can reach. The downloaded Skill includes that
address and commands for selecting the correct server.

Use the Go `lb` executable for the resource workflow. Substitute the address
your administrator has approved.

```sh
lb login https://lockbot.example.test
lb --origin https://lockbot.example.test skill install
lb --origin https://lockbot.example.test resource list
```

When `lb login` returns a code, confirm it with the bot in the target chat and
run the same login command again. After connecting, use `lb --origin
https://lockbot.example.test bind` to add another group.

Use `lb skill update` from the saved service to update the Skill. Check the
resource's supported operations before locking, booking, or releasing it. Use
the exact resource reference when node names overlap. Query the result after a
write before reporting success.

Set `RESOURCE_AUTH_V1_ENROLLMENT_ENABLED=false` to close new v1 enrollment.
Existing v1 keys retain access to their existing API scopes. The published Skill
uses v2 and is distributed as Markdown. See the
[v3.3.3 release documentation](https://github.com/dynamicheart/lockbot/blob/v3.3.3/docs/usage/resource-agent-helper.md)
for the previous release's Python client instructions.

The [API reference](/api/public-docs/agent-v2) and [HTTP quickstart](/api/public-docs/agent-v2/quickstart)
are served by the same deployment.
