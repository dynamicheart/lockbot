# Native legacy CLI read profile

The packaged CLI offers an explicit `--legacy-read` opt-in for existing private connections to the tested native API-1, Markdown-only distribution. The default modern discovery and mutation guards remain unchanged.

Use a verified installation of the current CLI and an existing connection with an approved origin, instance ID, bot UUID, principal platform/subject and private Key reference:

```sh
python /path/to/installed/scripts/lockbot_cli.py --legacy-read --connection /private/connection.json resources
python /path/to/installed/scripts/lockbot_cli.py --legacy-read resources-all --directory /private/connections
```

The flag belongs before the subcommand. `context` and anonymous `discover --origin ORIGIN` are also supported. This profile does not initialize connections, enroll, recover or rotate credentials, install the legacy Markdown ZIP, issue panel links, or access optional report/metrics services. All resource commands, alias/usage edits and POST/PUT/DELETE requests remain blocked, even when the manifest advertises related operations. Omitting the flag retains the modern service contract.

Before opening a Key, discovery requires the native service/API identity, the legacy `lockbot-resource-agent` descriptor, `lbr_bot_scoped` credential scheme, a nonempty pinned instance, the known discovery/version capabilities, the fixed same-origin context endpoint, and absence of a modern package or coherent snapshot contract. A legacy name or API-major equality alone cannot select the profile. An unknown descriptor, redirect or changed instance stops discovery without credential forwarding or rebinding.

Authenticated context must match the saved bot UUID and exact principal platform/subject. Bot ID, type and access state must be well formed; a saved bot-type pin is checked when present. The resource route must be either the fixed native agent resource endpoint or the bot resource route derived from that authenticated context. Each scope receives its own context check and separate resource read; names and overlapping subject strings never bridge scopes. Disabled access pauses resource reads; stopped bot health is retained.

Resource identity contradictions and malformed node/card records fail closed. Card zero remains valid. Optional hardware, queue and timing fields remain unknown when absent; malformed capture/age values become unknown. The observation retains health and reported capture time/age. The separate `snapshot_fresh_for_read` flag reflects valid capture/age timing and is aged at query completion. Instruction refresh requirements do not make an otherwise fresh read stale. Legacy snapshots always have `snapshot_fresh_for_write: false`: their read success cannot establish any mutation precondition. No persistent resource observation or node-access cache is created by this profile.

CLI bundle integrity and server instruction revisions are independent. The installed CLI still passes its local file and package integrity checks. Its package hash is not compared with the legacy Markdown revision as if the latter certified an executable. This profile does not load legacy instructions and sends no claimed legacy Skill/package revision. Output therefore reports `instruction_state: unknown`, or `changed` after an explicit update notice, and always reports `instruction_refresh_required: true` and `update_required: true`. Even a server's `current` notice cannot clear that local unknown state or enable a write.

Service/API metadata and instruction notices are inspected on success and HTTP error responses. Explicit API incompatibility stops the read. Existing request, response-size and 60-second limits apply; the call budget remains 12. Only bounded 429 reads honor Retry-After; transport failures and 5xx reads are not replayed. Multi-scope reads retain per-scope failures and continuation information when the remaining scopes were not visited. Compact ordinary output strips context links, instruction internals and optional telemetry authorization.

Synthetic packaged tests use a sanitized descriptor shape and localhost fixtures. They establish this client contract, not production credential validity, a deployed server change, or native legacy write compatibility. Servers without the required discovery contract remain unsupported.
