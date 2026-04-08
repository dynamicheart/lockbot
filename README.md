# lockbot

Cluster resource management bot for IM platforms (e.g., Baidu InfoFlow/如流).

Lock and unlock GPU devices, cluster nodes, and queue slots via chat commands.
Supports both standalone Flask deployment and a full platform mode with FastAPI + Vue.js frontend.

[中文文档](README_CN.md)

## Features

- **Device Lock Bot** — Lock/unlock individual GPUs or devices on a cluster
- **Node Lock Bot** — Lock/unlock entire cluster nodes
- **Queue Bot** — Manage a queue for resource allocation
- **Platform Mode** — Web UI (Vue 3 + Element Plus) for managing multiple bots,
  user authentication (JWT), role-based access control, and real-time logs
- **State Persistence** — Bot state survives restarts (file-based JSON)
- **Bilingual** — English and Chinese UI and bot responses

## Deployment Modes

### Standalone Mode (Flask)

Best for single-bot deployments. Runs a lightweight Flask webhook server.

1. Install:

```bash
pip install lockbot
```

2. Create a bot script (`bot.py`):

**Device bot** (lock by GPU):

```python
from lockbot.core.entry import run_bot
from lockbot.core.config import Config

Config.set('BOT_TYPE', 'DEVICE')
Config.set('BOT_NAME', 'my-gpu-bot')
Config.set("WEBHOOK_URL", "https://your-webhook-url")
Config.set("TOKEN", "your-bot-token")
Config.set("AESKEY", "your-aes-key")
Config.set("PORT", "8000")
Config.set('CLUSTER_CONFIGS', {
    'node0': ['a800', 'a800', 'h100'],
    'node1': ['a800', 'h100'],
})

if __name__ == '__main__':
    run_bot()
```

**Node bot** (lock by node):

```python
from lockbot.core.entry import run_bot
from lockbot.core.config import Config

Config.set('BOT_TYPE', 'NODE')
Config.set('BOT_NAME', 'my-node-bot')
Config.set("WEBHOOK_URL", "https://your-webhook-url")
Config.set("TOKEN", "your-bot-token")
Config.set("AESKEY", "your-aes-key")
Config.set("PORT", "8000")
Config.set('CLUSTER_CONFIGS', ['node0', 'node1', 'node2', 'node3'])

if __name__ == '__main__':
    run_bot()
```

3. Run:

```bash
python bot.py
```

### Platform Mode (FastAPI + Frontend)

Full management platform with web UI, multi-bot orchestration, user auth, and admin panel.

1. Install:

```bash
pip install lockbot
```

2. Set environment variables:

```bash
export JWT_SECRET="your-jwt-secret"
export ENCRYPTION_KEY="your-fernet-key"    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
export DEV_MODE="true"                      # auto-create admin user (dev only)
```

3. Start:

```bash
# Backend
uvicorn lockbot.backend.app.main:app --host 0.0.0.0 --port 8000 --reload

# Frontend (in another terminal)
cd frontend && npm install && npm run dev
```

### Docker

```bash
docker build -f docker/Dockerfile -t lockbot .

# Platform mode (default)
docker run -d -p 8000:8000 \
  -e JWT_SECRET=your-secret \
  -e ENCRYPTION_KEY=your-fernet-key \
  -v lockbot-data:/app/python/lockbot/data \
  -v lockbot-state:/data/bots \
  lockbot

# Standalone mode (mount your bot.py, persist state to /data/bots)
docker run -d \
  -v $(pwd)/bot.py:/app/bot.py \
  -v lockbot-state:/data/bots \
  lockbot \
  tmux new-session -d 'python /app/bot.py'
```

## Bot Configuration

| Config Key | Description | Default |
|---|---|---|
| `BOT_TYPE` | `DEVICE`, `NODE`, or `QUEUE` | (required) |
| `BOT_NAME` | Bot instance name | `demo_bot` |
| `CLUSTER_CONFIGS` | Cluster layout (dict or list) | `{}` |
| `TOKEN` | Bot token for signature verification | `""` |
| `AESKEY` | AES key for message decryption | `""` |
| `WEBHOOK_URL` | Webhook URL for sending messages | `""` |
| `PORT` | Server listen port | `8090` |
| `DEFAULT_DURATION` | Default lock duration (seconds) | `7200` (2h) |
| `MAX_LOCK_DURATION` | Maximum lock duration (seconds) | `-1` (unlimited) |
| `EARLY_NOTIFY` | Notify before lock expires | `false` |

See `python/lockbot/core/config.py` for the full schema.

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint + format
ruff check python/ tests/
ruff format --check python/ tests/
```

## License

MIT
