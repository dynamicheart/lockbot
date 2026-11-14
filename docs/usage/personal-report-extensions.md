# Personal report extensions

A deployment can expose reviewed local reporters through the same
`lockbot-resource-agent` Skill. Enable them only on the native identity
provider with an issuer equal to the configured panel origin.

Add `agent_report_extensions` to the private panel configuration. Each entry
has a unique lowercase slug in `id`, a short `label`, an opaque `revision`,
the local module `file`, its private `config`, and explicit `grants`.
Each grant contains the exact native provider values for `issuer`,
`platform`, `realm`, `subject`, `scope` and `ref`. The opaque scope includes
the instance, bot identity revision and credential generation when present.
Manage this file as server configuration and keep identity mappings outside Git.

The module exports `create_reporter(config)`, returning an object with
`report(grant)`. The grant is a read-only mapping. Use it to verify the
upstream identity and filter the report to that subject. Keep upstream
credentials in the deployment's secret store. Return at most 16 groups and
128 total allocations. A group includes `group_id`, `label`, `status`,
optional offset-bearing `source_at`, and `allocations`. Each allocation
contains `kind` (`hold` or `reservation`), `node`, optional `device` and
optional offset-bearing `expires_at`. Reports older than 5 minutes, or more
than 30 seconds ahead of the server clock, are treated as unknown.
Give the adapter its own upstream deadlines. The server budgets at most
four concurrent calls and waits up to five seconds per request. A timed-out
call continues to occupy its slot until the adapter returns.

Authenticated context advertises the caller's eligible extensions. The helper
reads an extension with `personal-report --extension <id>`, using the saved
connection for that same native service. Reload the service after removing an
extension, revoking a configured grant or changing adapter configuration.
Change `revision` when the configured contract or semantics change.

See [the protocol design](https://github.com/dynamicheart/lockbot/pull/165) for the
wire contract and its relationship to the browser dashboard.
