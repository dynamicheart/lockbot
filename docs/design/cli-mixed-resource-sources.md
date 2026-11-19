# Mixed resource sources: local CLI contract

This is a local candidate contract, not a deployed release. The native CLI can
read saved native connections and explicitly registered external providers in one
bounded traversal. External protocol implementations remain outside this public
package. Names and aliases are display/search facts, never source authority.

## Configure once, then use normal resource commands

Create a private configuration outside source control (mode 0600 on POSIX, or
the existing private-ACL policy on Windows):

```json
{
  "schema_version": 1,
  "native": {
    "directory": "/absolute/private/connections",
    "legacy_read": false
  },
  "providers": [
    {
      "registration": "/absolute/private/provider.json",
      "minimum_http_requests": 4
    }
  ]
}
```

`native` can be null. Up to eight providers may be registered. A provider's
registration uses the [explicit provider contract](../usage/resource-agent-helper.md#explicit-read-only-resource-providers).
The minimum is an admission estimate, not permission to exceed the remaining
budget. The same registration cannot be listed twice through aliases. Executable
registrations are explicit trusted local code, not programs discovered from
network responses. The CLI does not install providers or initialize credentials.

```sh
lockbot --resource-sources /absolute/private/sources.json resource list
lockbot --resource-sources /absolute/private/sources.json resource list --model P800 --limit 5 --json node,unit,hardware
lockbot --resource-sources /absolute/private/sources.json resource list --search 'P800 07' --limit 5 --json node,hardware
lockbot --resource-sources /absolute/private/sources.json resource list --mine --json node,holding_units,booking_units
lockbot --resource-sources /absolute/private/sources.json resource list --json
```

A local launcher or shell alias can supply the global configuration option, so
normal use remains `lockbot resource list`. The existing native-only behavior is
unchanged when the option is absent. Explicit provider mode remains available.
Do not combine native connection/directory/profile overrides with the sources
configuration; its native entry pins those settings.

The mixed list exposes common fields. A provider omits unavailable facts rather
than inventing values. Missing hardware, ownership or queue facts remain unknown.
The node selector uses observed IDs and any aliases the source actually supplies;
it does not require bot IDs or group names. Model filters use observed hardware.
Exact view commands retain the selected source's own field discovery.

Add `--with-metrics --json node,states,metrics` for optional compact utilization.
Native phases use their verified source configuration; a registered provider
must explicitly implement `reported_resource_utilization_v1`. These reported
resource percentages have no verified hardware sample time and do not identify
CPU/GPU families. Compute and memory coverage remain separate. A legacy provider
still returns ordinary inventory once, with an explicit metrics gap; the CLI
does not probe, retry or borrow native authority for it.

Metrics share the same request budget and deadline. Their read-completion and
retry fields are separate from inventory coverage. During later phases, the
parent ages native metric validity and reported-value visibility independently;
expired means are retired without turning a successful read into a failed
request. A missing metric in one source does not retire unrelated healthy values.
Source-read and formatting failures retain validated request costs when known.

Registration capabilities are part of the catalog cursor, but changing them does
not change an existing external resource locator. Full registrations are checked
again after the subprocess exits. Executable/config aliases cannot bypass
duplicate-source checks by declaring different capabilities. See the
[reported percentages contract](../usage/resource-agent-helper.md#reported-resource-percentages).

The optional literal `--search` filter follows the [native search contract](cli-resource-contract-draft.md#search-names-aliases-and-hardware).
Each source applies it to full observed permitted metadata before paging and field
selection. Successful searched provider pages must confirm the same canonical
query with `search_mode: literal_terms_v1` and `search_applied`; an older provider
cannot silently supply unfiltered rows. Missing matching evidence keeps coverage
incomplete. The parent never emulates search over an already bounded old page or
starts an extra capability-probe process. Unsearched query/wire shapes stay unchanged.

## One budget and one continuation

Each invocation shares at most twelve attempted HTTP requests and a sixty-second
checked deadline, reading native scopes first and then configured providers.
Provider subprocesses receive the remaining time and request budget. Inline
native network calls retain the standard-library blocking-I/O limitations of the
native reader; this is not a strict cancellation guarantee for DNS/trickled
headers. Native GET retries consume the same slice.

The list defaults to twenty rows, with a maximum requested limit of 256 and an
8 KiB whole-output cap. It stops when enough results are available or a later
source cannot fit its minimum request budget. It does not cache resource
snapshots. The JSON result contains `has_more` and `next_cursor`. Repeat the same
query, including the sources configuration, filters, selected fields and limit,
and add `--cursor NEXT_CURSOR`.

The outer cursor binds the source definitions, normalized query and current
source's inner position. Completed phases are not read again. A source crossing
pages is reread from its own live position; insertions before an old boundary
require a fresh traversal. A resumed page cannot prove a simultaneously fresh
global inventory. Changed source configuration or query rejects the outer cursor
before Key access. Each source still validates its own catalogue and principal.

Runtime row/byte budgets do not change the logical query or inner cursor identity.
Providers wrap public references before sizing their packets. The parent checks
the complete final representation, including cursor, framing and mandatory
expiry markers. It never advances past rows it did not return. A rare packing
refinement may reread the same position once with a smaller runtime row budget;
`packing_requeries` records it and all HTTP calls remain charged. It does not
retry server errors or mutations. An irreducible page, or an unaffordable packing
refinement, fails explicitly: reduce `--limit` or request fewer fields.

Source errors remain coverage gaps. A source returning an invalid continuation
cannot discard earlier healthy results or claim complete coverage. A declared
cost is counted exactly; unknown cost debits its allocated slice conservatively.
In that case `http_requests` is null, `known_http_requests` is only the known
lower bound, and `http_budget_used` is the conservative debit. No total request
count is fabricated from the allocation. Source retry delays are retained.

## References and time

Native references keep their existing `s-` form. External rows use an `e1` wrapper
binding the exact registration and its private configuration metadata to the
provider's opaque locator. The decoded locator is checked for secret-shaped
material and private paths as well as the encoded form. A wrapper is a locator,
not an identity mapping or authorization grant. Matching names never merge
credentials, principals or physical-machine counts.

```sh
lockbot --resource-sources /absolute/private/sources.json resource view '<returned ref>' --json node,hardware
lockbot --resource-sources /absolute/private/sources.json resource devices '<native ref>' --json id,model
lockbot --resource-sources /absolute/private/sources.json command --ref '<native ref>' --operation lock --whole-node
```

The final example is a resource mutation, requiring the normal authorized task
and fresh native checks. Native writes resolve only the pinned native catalogue,
and legacy profiles still block writes. External references cannot dispatch
native operations or switch to another registration; unsupported operations fail
before a provider process or Key access.

Row-evidence and coverage-evidence durations are independent. They bound snapshot
age and known expiry events, not guaranteed continued occupancy or write
authority. Missing lease timing does not turn a fresh verified booking into an
unknown identity or a permanent lease. Known deadlines still shorten validity.
The parent subtracts the whole phase duration conservatively, then rechecks at
final output. Expired state/own counts are withdrawn with a mandatory incomplete
marker; static locators and observed hardware remain. Empty own results also
need fresh coverage evidence before they can confirm zero.

An own-only source can answer its own-record query, but cannot establish full
fleet inventory or vacancy. Gaps, unclassified sources and expired coverage keep
`scope_complete` false. Every later native mutation independently verifies the
selected target online; concatenated query pages do not establish authority.
