# Resource CLI review draft

Status: local implementation for review. These options have not been deployed or added to the published Skill. The existing command names and default output remain compatible during this experiment.

The CLI should help a user find a machine without knowing a bot ID or relying on a group/bot name. Names and aliases can repeat. A resource reference identifies an exact native node within its saved service, instance, bot UUID and verified principal. It is a locator; each read still verifies the service and credential online.

## Try the implemented read contract

In these examples, `lockbot` means a launcher for the candidate's `scripts/lockbot_cli.py`, using a separate private connection directory. The examples do not create bindings or change resources.

```sh
lockbot resources-all --summary
lockbot resources-all --summary --model P800
lockbot resources-all --mine --summary
lockbot resources-all --json
lockbot resources-all --json node,unit,hardware,matched_cards,states --limit 10
lockbot resources --ref '<ref from a returned row>' --summary
lockbot resources --ref '<ref from a returned row>' --mine --summary
lockbot resources --ref '<ref from a returned row>' --summary --with-metrics
```

Bare `--json` lists supported fields without reading a Key or making HTTP requests. `--json FIELDS` selects summary fields and retains `ref`. A conflict marker is also retained when observations disagree, even if it was not requested as a field.

| Output | Meaning |
| --- | --- |
| `ref` | Exact node locator, independent of display names and aliases |
| `unit` | `whole_node`, `card`, or `unknown`; never infer card allocation from a name such as `dev0` |
| `hardware` | Observed hardware counts for the node, including other models |
| `total_cards` / `matched_cards` | Full node card count versus cards matching the requested model; unknown counts are null |
| `matched_device_ids` / `idle_device_ids` | At most 20 exact observed IDs; ID zero and leading-zero node names are preserved |
| `states` | Counts of idle, busy, shared, queued and unknown units; these are display categories, not allocation guarantees |
| `hardware_incomplete` | Some hardware facts are missing or invalid |
| `observation_conflict` | Separate credentials returned conflicting display observations; withhold device and state claims |

Summary output defaults to at most 20 nodes and is capped at 8 KiB including metadata and the printed newline. Byte limits can reduce the number of returned nodes. A single oversized row retains its reference and marks `detail_deferred`; use that reference to inspect the node. Arrays of device IDs and hardware categories are separately bounded and report when more exist.

Equal display observations of the same reference collapse to one row. This does not merge credentials, permissions, leases or physical machines across different scopes. An ambiguous local reference with multiple credential candidates requires an explicit existing connection in this draft.

`--mine --summary` groups only the verified caller's records by node. `holding_units` and `booking_units` count separate observations; at most 20 holdings and 20 bookings appear per node. Bookings retain their own queue rank and claimability and do not inherit the current holder's mode or lease. Missing ownership/queue data, ambiguous identifiers or an unknown allocation type cannot confirm zero personal occupancy. Conflicting same-scope observations, including an occupied versus empty own set, withhold the affected rows' claims.

## Operate on a selected reference

The local candidate also accepts an exact reference for a resource command:

```sh
lockbot command --ref '<ref>' --operation lock --whole-node --duration 1h
lockbot command --ref '<ref>' --operation lock --device 0 --model P800 --duration 1h
lockbot command --ref '<ref>' --operation release --whole-node
```

These commands change resources when used with a modern service; the examples are not part of the read-only replay. Choose either `--whole-node` or explicit device IDs. A reference targets its exact saved scope and node, without scanning other groups or replacing the node with a matching alias. The CLI still verifies the loaded scope pins, authenticated identity, fresh typed state, ownership, devices, model and applicable duration/resource limits. A conflicting or changed target stops before dispatch. Legacy read mode blocks all commands.

A coherent modern service normally needs one anonymous discovery, one scoped resource read and one command request. Modern services without coherent snapshots require separate protected reads. A successful reply must contain a fresh, consistent post-observation with the requested holding mode. A lost reply or inconsistent observation is an unknown outcome: the CLI does not replay the write and offers `resources --ref '<ref>' --mine --summary` for a targeted read-only check.

For one selected scope, `--with-metrics --summary` fetches telemetry for the bounded reference prefix, reuses that invocation's verified context and recomputes resource freshness after the optional read. CPU/card/memory/filesystem samples preserve missing, stale, failed and invalid states. Telemetry errors remain separate from resource-query coverage. Successful zero utilization never grants allocation authority. Metrics cards and filesystems are bounded separately; the full result still obeys the 8 KiB cap.

## Continue a large saved catalog

Each invocation retains the existing limit of 12 HTTP requests, a 60-second deadline and sequential reads. There is no new local resource snapshot cache. The CLI does not run an unbounded pagination loop.

```sh
lockbot resources-all --summary
# If scope_page.next_after_scope is present:
lockbot resources-all --summary --after-scope '<returned cursor>'
```

There are two independent kinds of continuation:

| Field / option | Scope |
| --- | --- |
| `scope_page.next_after_scope` / `--after-scope` | Continue unattempted saved connection records after the HTTP/deadline budget stops a read |
| `has_more`, `next_offset` / `--offset` | Display another portion of rows from a newly fetched observation |

`--offset` is not a persistent snapshot or server cursor. Resources can change between invocations, so it does not promise a consistent inventory across calls. Narrow a query by observed model/node or inspect an exact reference when possible.

The scope cursor contains a version, a hash of local catalog/provenance metadata and a record position. It contains no Key, raw local path or saved resource data. A changed catalog rejects an old cursor before HTTP. An incomplete catalog scan cannot issue a continuation cursor. Cursor validation does not read Key contents.

Attempted failures stay in that invocation's `unread` list. Continuation advances to unattempted records; it does not retry prior failures or bypass a server's `Retry-After`. Deduplication is local to each invocation, so equivalent saved records on different pages can return the same reference again.

`scope_page.exhausted` means the end of saved-record traversal. A resumed page always has `complete: false` (or `scope_complete: false` in summary output), including the final page. It does not prove all resources were freshly observed together. Mutation discovery cannot use cursors or joined pages to establish unique authorization.

## Review boundaries and remaining work

- Exact-reference commands are implemented and tested against a local packaged backend. Production writes and deployed-client verification have not been performed for this candidate.
- Summary mode rejects `--full`. Aggregate summary plus `--with-metrics` still needs a shared request-budget plan; scoped summary telemetry and bounded personal records are implemented locally.
- Server limits and per-bot admission caps are unchanged. Client traversal budgets and server contention protection solve different problems.
- The opt-in [native legacy read profile](cli-native-legacy-read-profile.md) validates known old discovery and scoped separate reads while blocking mutations. Its package and instruction states remain independent. Credentialed reads against a deployed older service still require their own evidence.
- External P800 reports remain a separate provider integration. Existing own-allocation bridges cannot supply fleet inventory, and native credentials/references cannot be reused as external authority.
- Candidate search, private labels, device-fact ingestion and the final human-readable list/view command layout remain separate work. The published Skill should follow the reviewed CLI contract.

The implementation borrows field discovery and bounded-list ideas from [`gh` formatting](https://cli.github.com/manual/gh_help_formatting) and [`gh repo list`](https://cli.github.com/manual/gh_repo_list). LockBot's shared resource contention requires its own server limits; GitHub's quota numbers are not adopted here.
