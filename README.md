# lockbot

Cluster resource management and queuing for Infoflow and Feishu.

Lock and unlock GPU devices, cluster nodes, and queue slots via chat commands.
Runs as a multi-bot platform with FastAPI and a Vue.js frontend.

[中文文档](README_CN.md) | [Live Demo](https://dynamicheart.github.io/lockbot/)

[![PyPI version](https://img.shields.io/pypi/v/lockbot?color=blue)](https://pypi.org/project/lockbot/)
[![Docker Image](https://img.shields.io/badge/ghcr.io-dynamicheart%2Flockbot-blue?logo=docker)](https://github.com/DynamicHeart/lockbot/pkgs/container/lockbot)

<img src="./docs/demo.gif" width="600" />

## Features

- **Device Lock Bot** — Lock/unlock individual GPUs or devices on a cluster
- **Node Lock Bot** — Lock/unlock entire cluster nodes
- **Queue Bot** — Manage a queue for resource allocation with booking and preemption
- **Device Queue Bot** — Book and queue for individual cards with `QUEUE_DEVICE`
- **Agent integration** — Personal authorization, resource discovery and versioned Skills
- **Utilization** — Optional browser/Agent access to independent Prometheus data sources
- **Platform Mode** — Web UI (Vue 3 + Element Plus) for managing multiple bots, user authentication (JWT), role-based access control, and real-time logs
- **State Persistence** — Bot state survives restarts (JSON file)
- **Bilingual** — English and Chinese UI and bot responses

## Quick Start — Docker

The published image includes the web UI and Feishu dependencies. Prepare a private environment file with `JWT_SECRET` and `ENCRYPTION_KEY` following the [deployment guide](docs/agents/deployment/AGENTS.md). For upgrades, keep the original keys and data volume.

```bash
docker pull ghcr.io/dynamicheart/lockbot:v3.0.0
docker run -d --name lockbot -p 8000:8000 \
  --env-file /private/path/lockbot.env \
  -v lockbot-data:/data \
  ghcr.io/dynamicheart/lockbot:v3.0.0

# New installation only: create the initial super administrator interactively.
docker exec -it lockbot python tools/create_super_admin.py --username admin --email admin@example.com
```

Open `http://localhost:8000`. Data persists under `/data`; select the intended host port without replacing an upgrade's existing mount. Run a single process/instance per data directory.

- [Agent-assisted deployment and upgrades](docs/agents/deployment/AGENTS.md), including environment validation and a Compose template
- [Infoflow and Feishu setup](docs/agents/platforms/AGENTS.md) and [illustrated Feishu guide](docs/usage/feishu-setup.md)
- [Agent API and Skill](docs/agent-api/SKILL.md)
- [Independent telemetry deployment](docs/usage/telemetry-deployment.md) and [browser utilization](docs/usage/browser-utilization.md)

In Feishu, one enterprise application can serve multiple resource groups, with a separate resource bot per group. Direct messages are unsupported. Platform credentials and users remain separate from Infoflow; copying configuration does not share live locks or authorizations.

## Bot Configuration

| Key | Description | Default |
|---|---|---|
| `BOT_TYPE` | `NODE`, `DEVICE`, `QUEUE`, or `QUEUE_DEVICE` | (required) |
| `BOT_NAME` | Bot instance name | `demo_bot` |
| `CLUSTER_CONFIGS` | Cluster layout (dict or list) | `{}` |
| `TOKEN` | Infoflow signature verification token | `""` |
| `AESKEY` | Infoflow message decryption AES key | `""` |
| `WEBHOOK_URL` | Infoflow message webhook URL | `""` |
| `DEFAULT_DURATION` | Default lock duration (seconds) | `7200` (2h) |
| `MAX_LOCK_DURATION` | Max lock duration (seconds) | `-1` (unlimited) |
| `EARLY_NOTIFY` | Notify before lock expiry | `false` |
| `FORBID_RELOCK` | Reject renewal and longer booking claims when explicitly enabled per bot | `false` |
| `MAX_RESOURCES_PER_USER` | Held + booked resources per user/bot; nodes for NODE/QUEUE, cards for DEVICE/QUEUE_DEVICE | `-1` (unlimited) |

See `python/lockbot/core/config.py` for the full configuration reference.

## Commands

| Command | Description |
|---------|-------------|
| `lock <node> [duration]` | Exclusive lock (e.g., `lock gpu0 3d`, `lock node1 30m`) |
| `slock <node> [duration]` | Shared lock (multiple users) |
| `unlock <node>` / `free <node>` | Release a specific node |
| `unlock` / `free` | Release all your nodes |
| `kickout <node>` | Force release (admin) |
| `book <node> [duration]` | Queue: book a node for later |
| `take <node>` | Queue: take the current lock |
| `<node>` | Query current usage |
| `help` | Show usage |

## Development

### Dev Server

```bash
# Install dependencies
pip install -e ".[dev,feishu]"
cd frontend && npm install && cd ..

# Start backend (port 8002, hot-reload)
bash scripts/dev/backend.sh

# Start frontend (port 8001, proxies /api/* -> localhost:8002)
bash scripts/dev/frontend.sh
```

Open the frontend at `http://localhost:8001`; it proxies API requests to port 8002. These hot-reload scripts are for development, not production. New development targets `main`.

### Testing

```bash
# Run tests with file-isolated parallel workers
pytest -n auto --maxprocesses=4 --dist=loadfile

# Lint + format check
ruff check python/ tests/
ruff format --check python/ tests/
```

## Migrating an older standalone deployment

The deprecated Flask entry (`lockbot.core.entry.create_app` / `run_bot`) and process-global
`Config.get/set/load_from_file/load_from_env` API have been removed. Bot construction no
longer starts a private scheduler. The supported deployment is the FastAPI platform above,
including when managing only one bot. Existing platform Docker deployments keep the same
startup command, ports and persistent data layout; no database reset is required.

For an old standalone deployment:

1. Keep a backup of its configuration and JSON state before stopping its old webhook server.
2. Start the platform, create the matching bot type, and copy its cluster, messaging
   credentials and runtime rules into the bot editor. Do not publish credentials in logs.
3. Restore the saved cluster state through the platform's existing cluster-state import
   while the bot is stopped. Verify resource names, holders, expiry and queues before start.
   Do not copy old state over an unrelated bot's directory or remove the old data volume.
4. Point the messaging platform callback at `/api/bots/webhook/{bot_id}` and start the bot.
   Keep the previous image and backup until the new callback and expiry checks work.

The core bot classes remain independently constructible for tests, with per-instance
`Config` objects. Platform scheduling, persisted state formats and legacy state migrations
remain supported. Independent utilization collection and Prometheus setup are documented
in [Resource exporter](docs/usage/resource-exporter.md).

## License

MIT
