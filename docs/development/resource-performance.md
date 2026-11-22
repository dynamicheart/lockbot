# Resource API performance benchmark

The benchmark checks that regular polling stays usable, excessive polling is limited, and allocation commands remain responsive. A burst from one Key must leave capacity for other callers sharing its IP and for people querying from their chat groups.

The benchmark uses a temporary SQLite database, synthetic credentials and 34 bots with 945 resource units. It listens on a loopback port. The fairness scenario also creates a synthetic Feishu group and configures an Infoflow callback on a fixture bot. Both callbacks go through validation, command dispatch, and rendering. The fixture intercepts replies at the platform transport boundary and validates their destinations and contents.

## Checks

| Scenario | Workload | Pass condition |
| --- | --- | --- |
| `mixed` | Regular filtered queries and competing writers | At least 99% of scheduled queries return complete and valid results. All write rounds pass. |
| `key-fairness` | One Key floods the resource endpoint while 20 other identities poll from the same IP. Infoflow and Feishu send three group queries each. | All peer reads and six group replies succeed. Excess requests from the flooding Key receive 429 or 503. All write rounds pass. |
| `poll-5s`, `poll-1s` | 500 identities schedule one minute of polling at each cadence | All write rounds pass. The report records successful reads, rate limits, busy responses, and client backlog. |

A write round checks that only one competing lock succeeds, another user can book, and cancellation and release succeed. Each request has a timeout. Latency is reported as p95 for comparison on the same runner.

The fairness scenario gives its fixture IP a smaller bucket of 80 requests per minute so CI can test source exhaustion in a short run. It sends a burst before starting the other users, then keeps the flooding Key active while those users and the chat callbacks run. The other scenarios use the benchmark's regular ingress settings. The benchmark gives its synthetic Keys 120 reads and 30 writes per minute to allow repeated write contention checks.

## Run locally or in CI

```sh
python -m pip install .
python scripts/dev/benchmark_resource_api.py --server-root . --profile smoke --output /tmp/resource-smoke.json
python scripts/dev/benchmark_resource_api.py --server-root . --profile weekly --output /tmp/resource-weekly.json
```

The smoke profile includes regular traffic and the fairness check. The weekly profile adds the five- and one-second polling scenarios and expands regular traffic to 500 identities polling every 30 seconds. GitHub Actions runs the weekly profile at 20:23 UTC on Saturdays. Both profiles are available on manual dispatch, and completed scenarios are recorded in the JSON artifact.

To compare ingress behavior across source addresses, use the same fixture and vary only the number of simulated addresses.

```sh
python scripts/dev/benchmark_resource_api.py --server-root . --profile weekly \
  --scenario poll-5s --scenario poll-1s --source-ips 8 --output /tmp/resource-eight-egresses.json
```

Simulated addresses are assigned at the fixture's ASGI boundary. The fairness scenario always uses one source IP. The report distinguishes planned requests, client backlog, HTTP outcomes and response latency. Deployment capacity depends on ingress settings, hardware and traffic patterns.
