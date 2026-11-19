# Find resources without a full inventory dump

Run the installed `scripts/lockbot_cli.py` by absolute path. The commands below omit
that Python/script prefix. Use saved authorized connections; a missing local catalog
is a setup problem, not evidence that no resources exist.

## Start with the user's question

| Request | First command |
| --- | --- |
| 看看能用什么 | `resource list --limit 5 --json node,unit,hardware,states` |
| 找台 P800 | `resource list --model P800 --limit 5 --json node,unit,hardware,states` |
| 资源都列一下 | `resource list --limit 256 --json node,unit,hardware,states`，继续返回的 `next_command` |
| 找一下 node07 | `resource list --search node07 --limit 5 --json node,alias,unit,hardware,states` |
| 我 lock 了哪些 / book 了哪些 | `resource list --mine --json node,holding_units,booking_units,holdings,bookings` |
| 这台有哪些卡 | `resource devices REF --json id,model,state` |
| 看下这台的利用率 | `resource view REF --with-metrics --json node,states,metrics` |

List rows always retain `ref`. Keep it for exact follow-up reads and authorized
commands; do not reconstruct it from a displayed name. Repeated node/robot names
may identify different resources. Describe the distinguishing node, alias or hardware
the user needs rather than asking them to find a bot ID. Search matches literal
words across observed node IDs, public aliases and known hardware; `--model` is the
hardware constraint. Names alone do not establish the model.

For a direct operation with a node hint, use the unscoped `command --node` path in
[resource operations](resource-operations.md) instead of adding a preliminary list.
For a search-and-lock request without a node hint, find a verified match, keep all
requested model/card/mode constraints, then use its exact reference for the command.
A list reference is a locator, not a reservation or proof of current ownership.

## Fetch detail only when needed

The default list is at most 20 nodes and the complete output is capped at 8 KiB.
For recommendations, a first page of five is usually enough; this is not a limit on
how many matches a user can request. Select the fields needed for the question.
Bare `resource list --json` or `resource devices --json` lists supported fields
locally without reading credentials or making requests. `--help` explains options.

Answer from fields already returned. Use `resource view REF` for missing detail;
do not repeat a read merely because a reference is available. Describe allocation
states as idle, held or queued; they do not establish machine health or successful
login. Device IDs in a list can be clipped;
Carry explicit unknown/partial markers into the reply, including when detail
reveals uncertainty absent from an earlier aggregate. A field omitted by JSON
selection is not evidence that its value is absent or zero.
use `resource devices REF`, optionally with `--model` or `--mine`, for the selected
node's cards. For an explicit request for many IDs, select only `id,model` and raise
`--limit` up to 256; the byte cap still applies. Never treat clipped arrays as the
entire card inventory. `--mine` separates current holdings from queue bookings;
neither the current holder's expiry nor a queue estimate guarantees a booking start.

Compatibility `resources` / `resources-all` and `--full` remain available for
diagnosis or details absent from the compact readers. Do not start ordinary queries
with a full dump or read the full JSON merely to answer a small question.

## Continue enough to answer

Use the returned `next_command` argument array to continue the same query. If only
`next_cursor` is supplied, repeat the original filters, fields and limit with
`--cursor`. Do not change filters while reusing a cursor. `has_more` means traversal
can continue, not that more matching rows are already known.

For “全部” or a complete own-resource list, continue through the remaining pages.
This read request already covers its continuation; do not stop to ask whether to
fetch the next page. Use explicit JSON fields so the returned continuation can be
followed directly. Bare `--json` only discovers fields, not resource rows.
If an initial page has no suitable candidate, continue before claiming no match.
If sources fail, are stale or lack hardware/ownership evidence, preserve those gaps:
`scope_complete:false` is not an empty result. `traversal_exhausted` only means the
traversal ended; it does not remove missing evidence. A resumed page is a new live
observation, so never present multiple pages as one simultaneous snapshot.

The CLI shares twelve HTTP requests and sixty seconds across one invocation,
including optional telemetry. One CLI invocation can make several HTTP requests.
It stops before reading another scope once the page is filled and creates no local
resource snapshot cache. Old services still return a whole scope upstream; compact
output limits model context, not those network bytes. Respect `Retry-After` and
report remaining coverage rather than launching competing copies of the query.

## Optional utilization

Add `--with-metrics` when load is part of the question, together with `metrics` in
selected JSON fields. Selecting the field alone does not fetch telemetry. Inventory
and metric coverage are separate: a failed metric read must not erase resources.
Native values include sample freshness and mapping coverage. Some external sources
only report percentages: `reported_mean_percent` and
`reported_memory_mean_percent` have no known hardware sample time. Their report age
must not be described as sample age. Missing, invalid or stale readings stay unknown;
a valid zero is still not proof of an idle, permitted or unqueued resource.
A zero without verified sample freshness is a reported reading, not a claim
about current physical load.

For source setup, grants or individual metric details, read
[workbench and metrics](workbench-and-metrics.md).
