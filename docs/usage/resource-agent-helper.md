# Install and use the LockBot Skill

Download [lockbot.zip](/api/agent/v2/skill/lockbot.zip) from your LockBot service.
It contains the Codex skill directory with `SKILL.md` and `agents/openai.yaml`.
The Agent API v2 manifest at `/api/agent/v2/manifest` lists the ZIP and its SHA-256.
Set `PLATFORM_URL` at deployment to the HTTP or HTTPS address that Agents can
reach. The downloaded Skill includes that address and commands for selecting
the correct server.

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

New v1 enrollment is disabled by default. New clients bind through Agent API v2.
Existing v1 keys retain access to their existing API scopes. The published Skill
uses v2. See the
[v3.3.3 release documentation](https://github.com/dynamicheart/lockbot/blob/v3.3.3/docs/usage/resource-agent-helper.md)
for the previous release's Python client instructions.

The [API reference](/api/public-docs/agent-v2) and [HTTP quickstart](/api/public-docs/agent-v2/quickstart)
are served by the same deployment.
