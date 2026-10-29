# Managed hosting and assisted deployment

Lockbot can run on a managed container service **with one continuously running instance and a persistent disk**. Render is the first concrete template here; Railway and Fly.io are alternatives. This is assisted deployment: a button can create infrastructure, but it cannot approve enterprise permissions, make private networks reachable, or prove message delivery.

Provider documentation and pricing checked **2026-10-29**. The template has local schema validation only; no paid resources or cloud account were created for this research.

## Why the whole service needs these constraints

The [backend lifecycle](../../python/lockbot/backend/app/main.py) owns bot recovery, timers and the broadcast worker. [State ownership](../../python/lockbot/backend/app/state_ownership.py) takes exclusive locks because the allocator also keeps state in memory. SQLite, bot state and logs must persist together under `DATA_DIR=/data`; the [Dockerfile](../../docker/Dockerfile) serves the bundled frontend and API through one Uvicorn process on port 8000.

Do not increase replicas or Uvicorn workers. The template explicitly sets `WEB_CONCURRENCY=1` to avoid a hosting default selecting multiple workers. An external SQL database alone would not make timers, bot state files and allocation coordination distributed. Do not enable scale-to-zero: expiring locks, reminders, queued broadcasts and Feishu event reception must run while no browser is open.

## Platform comparison

| Platform | Current capability | Fit for this Lockbot architecture |
| --- | --- | --- |
| Vercel, entire service | Container Runtime and WebSockets exist; Daemons can keep background processes running. However, HTTP containers scale down without traffic and Daemons have ephemeral storage without persistent storage. | Not a supported whole-service template. Splitting HTTP Functions from a Daemon does not share the current in-memory allocator. |
| Vercel, frontend only | Static hosting can serve built assets. | Possible only after explicitly configuring a separately hosted API, authentication, routing, CORS and public URLs. The current bundled frontend is the simpler supported route; no split-hosting template is claimed here. |
| Render | Paid web service with attached persistent disk; disk supports one instance and deployment stops the old instance before starting the new one. | Recommended initial template. Accept brief deployment downtime and configure independent backups. |
| Railway | Docker service plus persistent volume; volumes do not support replicas and redeploys incur downtime. | Viable alternative: one replica, /data volume, sleeping/serverless disabled, private secrets and HTTP health check. |
| Fly.io | Persistent local volume attached to one Machine; volumes do not automatically replicate between Machines. | Viable for experienced operators accepting a single Machine failure domain. Keep one Machine, disable autostop, and maintain independent backups. |

These are based on the official [Vercel Container Runtime](https://vercel.com/docs/functions/runtimes/container), [Vercel Daemons FAQ](https://vercel.com/docs/daemons), [Render disks](https://render.com/docs/disks), [Railway volumes](https://docs.railway.com/volumes/reference), and [Fly volumes](https://fly.io/docs/volumes/overview/). Do not use an HTTP uptime ping as a substitute for an always-running service contract.

### Cost and free-tier boundaries

- Render's checked entry paid compute is **$7/month for 512 MiB**, plus **$0.25/GB/month** disk. A 1 GB disk makes the template's base approximately **$7.25/month**, excluding traffic, build usage, taxes and other services. This is a starting size, not a capacity guarantee; monitor memory and disk. Free web services cannot attach persistent disks and can sleep, so they are unsuitable here. [Pricing](https://render.com/pricing), [free-service limits](https://render.com/docs/free).
- Railway Hobby is **$5/month including $5 of resource usage**; excess usage is additional. Volume storage is listed at **$0.15/GB/month**. This is usage-based, not a guaranteed $5 all-inclusive deployment. [Plans and resource pricing](https://docs.railway.com/pricing/plans).
- Vercel Daemons being available on Hobby does not provide durable local state; continuously provisioned daemon memory also consumes usage. Fly capacity and persistent volumes are metered. Check the provider's current quote before authorizing creation. [Vercel Daemons](https://vercel.com/docs/daemons), [Fly pricing](https://fly.io/docs/about/pricing/).

## Render template

[render.yaml](render.yaml) uses the existing Dockerfile, one instance, /data, a paid disk and `/api/health`. Auto-deployment is off: upstream pushes must not unexpectedly restart every installation. The schema accepts `0.5c-512mb`; Render may still display the legacy Starter name. The template intentionally has no branch pin: select and record a reviewed release commit in your fork before creating the service.

1. Fork or copy the reviewed repository. For an ordinary Blueprint, select `docs/hosting/render.yaml` as its Blueprint path. For a Deploy to Render button, copy this template to **render.yaml at your fork's root**. Preserve the repository-root Docker build context.
2. Generate keys privately with `python tools/gen_keys.py` in an installed Lockbot environment. Enter its **Fernet ENCRYPTION_KEY** when prompted; JWT_SECRET can be generated by Render. Export both to your protected recovery store. Never regenerate either merely to upgrade.
3. Review region, charges, disk and one-instance configuration. Create only after approval. Do not add a pre-deploy migration or admin hook: Render disks are unavailable to pre-deploy commands. Normal application startup runs migrations after acquiring state ownership.
4. After startup, use the service's private shell, verify `DATA_DIR=/data`, then run `python tools/create_super_admin.py`. Keep the generated password private and change it at first login. This shell is attached to the running service, unlike a diskless one-off job.
5. In site settings set the deployment URL to the actual HTTPS service URL. Configure allowed platforms, enterprise application credentials, proxy if needed, and resource-group bindings. A local or private callback URL is not reachable merely because the UI is public.
6. Verify health, login, bot state and persistence over a controlled restart. In an authorized test group verify query, short lock, queue, expiry/reminder, Agent binding and generated links. Confirm only one event consumer is active. Check runtime logs without publishing secrets.

Official Lockbot 3.x images include the Feishu implementation and optional SDK dependencies. Custom Python installations need `lockbot[feishu]`. Verify the selected image/version; this hosting template alone does not configure enterprise permissions, outbound connectivity or group associations.

Render's official [Blueprint specification](https://render.com/docs/blueprint-spec) and [Deploy button documentation](https://render.com/docs/deploy-to-render) define the fields and button behavior. The following is a **button example for your fork after adding root render.yaml**, not an active deployment button for this repository:

```markdown
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/YOUR_ORG/YOUR_FORK)
```

No undocumented `blueprintPath` query parameter is assumed. Enterprise permission approval, billing review, initial secrets, administrator bootstrap and acceptance remain explicit steps.

### Validate before creating resources

Download the official schema and validate locally (Python packages: PyYAML and jsonschema):

```sh
curl -fsSL https://render.com/schema/render.yaml.json -o /tmp/render.schema.json
python -c 'import json,yaml,jsonschema; jsonschema.validate(yaml.safe_load(open("docs/hosting/render.yaml")), json.load(open("/tmp/render.schema.json"))); print("Blueprint schema valid")'
```

Schema validation confirms the configuration shape, not provider capacity, Docker build success, application memory requirements or an authenticated message round trip.

## Network, metrics and recovery

A cloud-hosted Lockbot does not automatically reach a company's private IM callback infrastructure, internal proxy or metrics network. An intranet proxy address must be reachable from the cloud host before configuring it. Feishu HTTP API and WebSocket transport both need outbound connectivity; an incoming HTTP webhook requires a reachable HTTPS callback and platform verification. Preserve existing platform transport isolation.

Metrics remain independent: `SMI → exporter → Prometheus → read-only gateway → browser / Agent`. Prometheus does not parse vendor XML. The hosted Lockbot neither needs MPI nor collects hardware metrics in its web process. Deploy collectors where machines are reachable; the [telemetry guide](../usage/telemetry-deployment.md) covers collection, gateway and signer deployment. The [browser guide](../usage/browser-utilization.md) covers templates and node/card mapping.

The viewer's network must reach the metrics gateway. Public HTTPS pages cannot simply fetch private HTTP endpoints: TLS, mixed-content restrictions, CORS and routing still apply. Short-lived `metrics:read` grants authorize a query; they do not create a network route. Do not expose raw Prometheus or write/admin APIs to make the UI work. Keep source-specific grant-signing files outside bot data, securely provisioned to both the approved Lockbot issuer and the corresponding gateway.

Use the provider's supported connection-draining and shutdown-grace settings where needed, and allow in-flight commands and background work to finish before shutdown. Render's stop-before-start disk handoff prevents simultaneous mounts; it does **not** prove that the application completed a clean shutdown or produced a consistent backup. Verify shutdown logs and recovery separately.

Back up the database **and bot state from the same stopped allocator**, plus separately protected encryption/signing keys. Verify restoration into an isolated environment before replacing production. Render has daily disk snapshots retained seven days, but specifically warns that disk snapshots are not a substitute for database-native recovery. Fly likewise warns against relying solely on volume snapshots. A successfully restarted HTTP process is not evidence that locks, queues and credentials survived. [Render snapshot limits](https://render.com/docs/disks), [Fly volume backup guidance](https://fly.io/docs/volumes/overview/).

[Agent operating instructions](AGENTS.md) define the boundaries for assisted deployment. Managed hosting reduces host maintenance; it does not remove Lockbot's single-writer and private-network constraints.
