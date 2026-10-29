# Deploy and upgrade with an Agent

## Establish the deployment identity

Obtain the target host/platform, new install versus upgrade, selected published image, existing container/service name, published port, data mount, original encryption/JWT configuration and backup location. Ask only for missing facts; reuse approved deployment records without exposing their contents. Do not ask users to send secrets in chat.

Lockbot requires a long-running **single process** with a persistent `/data` directory. SQLite, resource state, keys and rotating evidence files are not disposable container storage. Do not configure multiple Uvicorn workers, replicas or overlapping old/new receivers against one data store. Feishu's WebSocket receiver and resource timers must survive ordinary requests; an HTTP health response alone does not prove either works.

Use the existing deployment method when upgrading. The Compose example is a reproducible option for new installations, not a reason to replace an existing mount, port, proxy or secret configuration. Public hosting feasibility is a separate design decision.

## Prepare configuration without deploying

The host needs Docker Engine and the Docker Compose plugin for the template workflow. Python 3.10+ is sufficient for the read-only preflight helper; generating keys with `tools/gen_keys.py` also needs the project's `cryptography` dependency. Use an existing project environment or the selected official image rather than changing the host's system Python packages.

From the repository root, copy `docs/examples/lockbot-deployment/settings.env.example` to a user-selected location outside Git and make it private (`chmod 600`). Replace the image placeholder with a published version or image digest. For a new installation, the existing `tools/gen_keys.py` generates both keys; direct its output to an approved private file instead of returning it in a tool transcript. On an upgrade, retain the original keys. Do not run a generator over the existing file.

For example, after selecting/pulling a verified image into `LOCKBOT_IMAGE`, a new installation can generate keys without starting the server or attaching a data volume:

```sh
umask 077
docker run --rm --entrypoint python "$LOCKBOT_IMAGE" tools/gen_keys.py \
  > /private/path/new-install-keys.env
```

Use a new, approved output path. Copy those two values privately into the settings file; do not run this command over an upgrade's original keys. This command starts a disposable key-generation process, so it is not part of the read-only preflight validation performed below.

The environment file accepts plain `KEY=VALUE` lines, not executable shell. Generate secrets using a trusted local runtime, then validate:

```sh
python tools/deployment_preflight.py --env-file /private/path/settings.env
```

For an existing host-visible data directory:

```sh
python tools/deployment_preflight.py --env-file /private/path/settings.env \
  --upgrade --data-dir /private/path/existing-data
```

The helper uses only the standard library. It reads configuration and, when selected, performs a read-only SQLite check. It never creates keys, writes a database, starts Docker, probes the network or prints secret values. A passing result is **not** a backup, proof of migration compatibility, network test or evidence that an image exists. Do not mount or copy a live Docker volume merely to satisfy this optional check.

If Docker Compose is available, validate its interpolation without printing resolved secrets:

```sh
docker compose --env-file /private/path/settings.env \
  -f docs/examples/lockbot-deployment/compose.yaml config --quiet
```

Do not run plain `compose config` into a public transcript: the resolved environment contains secrets. The template binds localhost by default. Select the user's intended bind address and published port explicitly, and configure an approved reverse proxy/TLS or network policy if remote access is required. Lockbot's browser-to-metrics-source CORS and source authorization remain separate.

## Execute only the authorized deployment

After the configuration is concrete and reviewed, an authorized new Compose deployment can use:

```sh
docker compose --env-file /private/path/settings.env \
  -f docs/examples/lockbot-deployment/compose.yaml pull
docker compose --env-file /private/path/settings.env \
  -f docs/examples/lockbot-deployment/compose.yaml up -d --no-deps lockbot
```

For upgrades, first stop the old writer and keep an application-consistent database/state backup, original image and original secret configuration. Avoid copying only a live SQLite main file while omitting WAL contents. Never use `down -v`, remove the volume or reset the database to get a new image running. Do not start the old receiver again while the new one is active.

Create the initial super administrator only for a confirmed new installation, using the existing tool in an interactive private terminal:

```sh
docker compose --env-file /private/path/settings.env \
  -f docs/examples/lockbot-deployment/compose.yaml exec lockbot \
  python tools/create_super_admin.py --username admin --email admin@example.com
```

The tool prints a generated password and may promote an existing username. Confirm the intended identity first; do not pass `--password` in command arguments or capture the generated password into a shared transcript. Do not automatically reset an existing administrator account.

## Verify the outcome

1. Check process/image identity, published port and the selected persistent mount, without dumping container secrets. Check `/api/health` at the intended service address.
2. Sign in through the intended service. Check bot configuration, active locks/queues, original keys and per-bot access. Startup success does not prove configuration/state preservation.
3. For Feishu, confirm the image includes its optional SDK dependency, the configured proxy can reach HTTP and WebSocket services, and exactly one receiver serves an application. Stop an old development receiver before moving the app connection. The dedicated HTTP probe does not prove WebSocket handshake or inbound delivery.
4. Use only an authorized test group and fictional resources for actual mention/query, short lock/release, queue/reminder, Agent binding and resource-link acceptance. Do not broadcast or mention everyone as an incidental health test.
5. Record image/tag/commit/checksum, backup identifier, health and acceptance outcomes, and limitations. Mark failed/uncertain sends as uncertain; do not automatically replay resource commands.

## Recovery

Stop the failing writer, preserve its current database/state/logs for diagnosis, and consult [upgrade and rollback boundaries](../../design/upgrade-rollback.md). Do not automatically overwrite current data with a backup: doing so loses changes since that backup. Older code may open an additive schema but fail to handle Feishu or restore an older, less restrictive privacy policy. A rollback requires its own explicit compatible version and state decision, not just a database integrity check.
