# Resource API performance benchmark

The benchmark starts the Agent API v2 on an ephemeral loopback port with a temporary SQLite database. The synthetic inventory has 34 bots and 945 resource units across node, device, and queue configurations. Each simulated identity has access to all fixture groups. Concurrent writers compete for the same card, then exercise booking, cancellation, and release.

From the repository root, install the backend in a virtual environment and run either profile.

```sh
python -m pip install .
python scripts/dev/benchmark_resource_api.py --server-root . --profile smoke --output /tmp/resource-smoke.json
python scripts/dev/benchmark_resource_api.py --server-root . --profile weekly --output /tmp/resource-weekly.json
```

- `smoke` runs 20 filtered reads and two rounds of competing writes.
- `weekly` runs 500 identities querying every 30 seconds, five seconds, and one second in separate phases. Each phase offers one minute of traffic from a single source IP and includes competing writes.

Both profiles require at least 99% complete and valid responses in the first phase and check write results in every phase. The five- and one-second phases record 429s, 503s, and client backlog as measured outcomes. Read p95 measures elapsed time from the scheduled request time to receipt of a valid response, including the client queue. The JSON report also records response sizes and write latency. Compare revisions on the same runner under the same profile. The fixture covers a repeatable workload, and deployment capacity depends on hardware, ingress settings, and traffic patterns.

The GitHub Actions workflow runs the weekly profile at 20:23 UTC on Saturdays and offers both profiles on manual dispatch. Each run uploads its JSON report.
