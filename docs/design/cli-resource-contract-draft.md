# Resource CLI review draft

Status: local implementation for review. These options have not been deployed or added to the published Skill. The existing command names and default output remain compatible during this experiment.

The [mixed-source contract](cli-mixed-resource-sources.md) adds explicit configured
native/external traversal with a shared budget and source-bound references.
The [private-label contract](cli-private-resource-labels.md) adds local annotations
and bounded label lookup without resource requests or shared alias changes.

The CLI should help a user find a machine without knowing a bot ID or relying on a group/bot name. Names and aliases can repeat. A resource reference identifies an exact native node within its saved service, instance, bot UUID and verified principal. It is a locator; each read still verifies the service and credential online.

## Start with a bounded resource list

The new local entrypoint is `resource list`; `resource view REF` reads a selected resource. Lists use a compact terminal table by default. Select JSON fields for agent use:

```sh
lockbot resource list
lockbot resource list --model P800 --json node,unit,hardware --limit 5
lockbot resource list --search 'P800 07' --json node,unit,hardware --limit 5
lockbot resource list --mine --json node,holding_units,booking_units
lockbot resource list --model P800 --with-metrics --json node,states,metrics
lockbot resource list --json
lockbot resource view '<ref from a returned row>'
lockbot resource view '<ref from a returned row>' --json node,unit,hardware,matched_device_ids
```

The list stops before reading another scope once its row or byte budget is filled. It defaults to 20 nodes and retains the 8 KiB whole-output cap. Large rows can be deferred to a smaller field selection. Device IDs, hardware categories and own records keep their independent bounds.

JSON returns `next_cursor` and normally a ready-to-run `next_command` argument array. Execute that command to continue. For unusually large query/identifier framing, the command array is omitted to keep output bounded; repeat the original query with `--cursor NEXT_CURSOR`. The cursor is tied to normalized filters, selected fields, limit, principal/profile and the local catalog. A changed query or catalog is rejected before any Key read or HTTP request. An incomplete/unavailable local catalog is rejected; the compatibility readers remain available for diagnosis.

Continuation skips fully consumed scopes. If a page ends inside one scope, the next call rereads that scope and continues after its last emitted exact node ID, ordered by UTF-8 bytes. Frozen observations traverse without lost or duplicate rows. Every page is a new live observation: insertions before the prior boundary require a fresh traversal, and a resumed tail never proves a complete, simultaneously fresh inventory. `has_more` means traversal can continue, not that further matches are already known. `traversal_exhausted` is independent of freshness, missing hardware and read failures.

Before emitting a scope, the CLI checks its locally recorded same-scope credential peers. Contradictory observations retain `observation_conflict`. A failed or budget-limited peer leaves locator-only rows with `observation_incomplete`; neither field selection nor a page boundary can turn them into state, device or ownership claims. Credential cohorts exceeding the budget can yield conservative partial results and advance without an unbounded retry loop. References still require an explicitly selected connection when multiple credentials match.

The list is sequential, shares the unchanged 12-request/60-second budget and never creates a resource snapshot cache. It currently reads each old API scope's full response; client-side paging does not reduce those response bytes. Exhaustive scans can reread a scope when its rows cross pages. Server-side filtering/paging is separate future work. Native list telemetry is opt-in with `--with-metrics`; `resource view REF --with-metrics` retains the bounded single-scope detail path. Mixed-source/provider list telemetry is not implemented and rejects the flag before source dispatch.

### Optional utilization in the same invocation

`--with-metrics` adds a compact CPU reading and accelerator summary, without raw
vectors, card arrays or filesystem arrays. The accelerator mean uses only valid
`ok` samples. `ok_devices`, `selected_devices`, `non_ok_devices`, `sample_states`
and `selection_complete` explain its coverage; missing or failed samples never
become zero. Card groups use the full validated model/own selection, including
IDs beyond the twenty-item overview. Whole-node card samples remain source
observations with unknown physical coverage, even when all reported samples are
healthy. A model filter on a whole node does not identify each source card's model.

All same-scope credential peers finish their resource reads before metrics.
Conflicting resource rows are not queried. The full metric selection and datasource
configuration are compared beyond the display prefix; disagreement withholds that
node's metrics. One verified credential performs the source read with its own
context/grant. Only a candidate display prefix is queried; later byte packing can
return fewer rows, without another HTTP request or skipping their continuation.

Resource reads and optional source requests, including authentication refresh and
failed requests, share twelve HTTP calls and sixty seconds. An exhausted budget,
unsupported source, `429` or source failure keeps resource rows. The separate
`metrics_read_complete`, `metrics_unread_reasons` and `metrics_retry_after_seconds`
describe source-read gaps; they do not rewrite resource `scope_complete`. A complete
read does not mean every card was mapped, fresh or healthy. Resource state and
sample ages are recomputed at final output. Valid zero utilization never establishes
that a resource is free.

Selecting `--json node,metrics` alone does not fetch telemetry. The explicit flag
controls fetching independently of field selection. Bare `--json` remains local
field discovery. The active flag is bound to the cursor and repeated in the next
command; omitting it preserves the previous non-metrics cursor format.

### Search names, aliases and hardware

`resource list --search TEXT` matches literal words against observed node IDs,
public node aliases and known hardware model labels. Each word must occur in at
least one of those fields within the same observation. For example, `P800 07`
can match model `P800` plus node `node07`; it also matches an actual node named
`P80007`. Add `--model P800` when the hardware model itself must be P800. Search
does not narrow the displayed card inventory; the exact model filter does.

Search uses NFKC and case folding, collapses whitespace, and sorts/deduplicates
at most eight words. Both raw and normalized input are limited to 160 UTF-8
bytes; non-whitespace control/format characters are rejected. Punctuation is
literal, with no regex, spelling correction, ranking or identity rewriting.
Search combines with `--node`, `--model` and `--mine`; exact view, device and
mutation commands do not accept it.

All observed allowed fields participate before display clipping, selected JSON
fields and pagination. Models beyond the first twenty displayed categories and
aliases beyond their displayed prefix can match. With `--mine`, only the hardware
evidence in the caller's own records is searched. Group/bot names, owners, task
notes, metrics labels, private tags and secret-shaped values are excluded. Terms
from conflicting credential observations are never pooled into a synthetic match;
a matching conflict retains its mandatory locator/uncertainty marker.

Unmatched nodes with missing hardware may still match an unknown model. Native
output reports `search_unknown_nodes` and keeps coverage incomplete, including
empty pages and combined model/search filters. A known node/alias match does not
require guessing its hardware. An absent search preserves the old query and
cursor hash. A changed canonical search rejects an old cursor before protected
reads. Search adds no HTTP requests or persistent resource cache; exhaustive
matching still needs to read the selected sources within the shared budget.

The same conservative rule applies to `--model` alone: missing hardware prevents
complete matching coverage, even when the definite known matches remain visible.
Unfiltered resource-list coverage does not require a guessed hardware model.

An external provider must acknowledge the exact canonical search using
`search_mode: literal_terms_v1` and `search_applied`. Older providers that ignore
the option cannot return unfiltered rows as search results. They leave a source
gap, with validated actual request cost retained and earlier healthy rows kept.

## Inspect many cards on one node

`resource devices REF` pages the observed card inventory for one exact selected node. It does not derive a card number from a node name such as `dev0`; whole-node groups use `resource view` for hardware facts.

```sh
lockbot resource devices '<ref>'
lockbot resource devices '<ref>' --model P800 --json id,model,state
lockbot resource devices '<ref>' --mine --json id,held_by_you,booked_by_you
lockbot resource devices '<ref>' --limit 200 --json id,model
lockbot resource devices '<ref>' --with-metrics --json id,model,state,metrics
lockbot resource devices --json
```

The default is at most 20 cards and 8 KiB for the complete output. Exact `id` is always retained. For an explicit inventory of many IDs, request only needed fields and raise `--limit` (maximum 256); the byte cap still applies. A 200-card coherent localhost fixture returned all IDs and models in one CLI invocation, two GETs and 5,474 bytes. Paging that same fixture at 20 rows used ten invocations and twenty GETs. Neither comparison means that a live server provides transactionally consistent state across calls.

Canonical decimal IDs sort numerically, preserving zero and gaps. Opaque IDs such as `00` retain their spelling and sort separately. Canonical collisions such as numeric `0` and string `"0"` are rejected before model or ownership filtering. Invalid IDs are counted as unidentified, never renumbered or replaced with array positions. Cursor validation precedes protected reads; it binds the exact target, selected credential metadata, profile, fields, filters and limit. Missing boundary IDs can be skipped by keyset order on a new observation; new IDs before the boundary require a fresh traversal.

`held_by_you` and `booked_by_you` are null when ownership evidence is missing, malformed or stale. `--mine` retains those unknown rows with `ownership_match_unknown`; it does not declare them owned or turn unknown into zero. The marker survives JSON field selection. Queue position and claimability describe only a uniquely identified own booking, not a current holder's lease. Raw owner identities, notes and expiry fields are not emitted. `total_devices` and `matched_devices` count observed configuration records, never free or authorized cards; matching counts are null when matching coverage is incomplete.

Device continuation uses `next_cursor`/`next_command`, without a local resource cache. A source snapshot is still reread for each page. `--with-metrics` reads the selected node's existing metrics once, reusing its verified context, then attaches only each returned inventory ID's explicitly mapped reading. IDs beyond twenty remain joinable. Opaque IDs unsupported by the mapping schema stay unknown; no source label or UUID becomes a guessed resource ID. Field discovery, byte packing and resource-aging rebuilds never repeat the source request. A resumed page never asserts complete inventory coverage, and legacy read mode remains unable to write or query telemetry.

Device pagination currently bounds output, not upstream source series. The existing
gateway query covers the node, retaining the client limits of 2,048 series and
1 MiB per response. Oversized responses leave a metrics gap and preserve inventory;
the CLI does not retry with guesses or claim device-level network pagination.
Selective source queries need an advertised gateway capability and remain pending.

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

Within one invocation, connections to the same approved origin and pinned instance can reuse a validated anonymous service descriptor. Protected context, principal checks, resource observations and telemetry grants remain separate for each credential. Discovery notices are retained, scope failures invalidate reuse, and a new invocation discovers again. If the remaining request budget cannot finish the next scope's minimum protected reads, continuation leaves that scope unstarted.

```sh
lockbot resources-all --summary
# If scope_page.next_after_scope is present:
lockbot resources-all --summary --after-scope '<returned cursor>'
```

The compatibility `resources-all` reader retains two independent kinds of continuation; prefer `resource list` for result-driven paging:

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
- Summary mode rejects `--full`. Native `resource list` and `resource devices` support optional telemetry under the shared budget; compatibility `resources-all --summary --with-metrics` and mixed/provider telemetry are not enabled.
- Scoped card telemetry uses explicit datasource `deviceMapping`, keeping source labels separate from resource IDs. Missing or ambiguous mappings stay unknown; mapping changes during short-grant refresh cannot be combined with an earlier resource snapshot. The detailed mapping contract is in the [helper guide](../usage/resource-agent-helper.md#best-effort-metrics).
- The collector candidate retains explicit XML `product_name`/`uuid` fields as bounded, timestamped device observations. Existing metric labels and collection cadence are unchanged. Missing or conflicting facts stay unknown. This local capture does not register hardware, establish trusted inventory, or provide a new API; authenticated ingestion and actual driver acceptance remain pending.
- Server limits and per-bot admission caps are unchanged. Client traversal budgets and server contention protection solve different problems.
- The opt-in [native legacy read profile](cli-native-legacy-read-profile.md) validates known old discovery and scoped separate reads while blocking mutations. Its package and instruction states remain independent. Credentialed reads against a deployed older service still require their own evidence.
- External protocol implementations remain private plugins. The explicit generic provider entrypoint and mixed-source traversal are implemented locally; existing own-allocation bridges still cannot establish full fleet inventory. Native credentials/references cannot be reused as external authority.
- Literal candidate search, private labels, paged device telemetry and the terminal list/view entrypoint are implemented locally for review. Trusted device-fact ingestion, provider telemetry and source-side selective device queries remain future work. The published Skill should follow the reviewed CLI contract.

The implementation borrows field discovery and bounded-list ideas from [`gh` formatting](https://cli.github.com/manual/gh_help_formatting) and [`gh repo list`](https://cli.github.com/manual/gh_repo_list). LockBot's shared resource contention requires its own server limits; GitHub's quota numbers are not adopted here.
