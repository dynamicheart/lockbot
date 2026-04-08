# lockbot

集群资源管理机器人，支持通过即时通讯平台（如百度如流）的对话命令锁定和释放 GPU 设备、集群节点和队列资源。

支持两种部署方式：单机 Flask 独立部署，以及 FastAPI + Vue.js 前端的管理平台模式。

[English](README.md)

## 功能特性

- **设备锁机器人** — 按单个 GPU/设备维度锁定和释放
- **节点锁机器人** — 按整个集群节点维度锁定和释放
- **队列机器人** — 管理资源分配队列
- **平台模式** — Web 管理界面（Vue 3 + Element Plus），支持多机器人管理、
  用户认证（JWT）、角色权限控制和实时日志
- **状态持久化** — 机器人状态重启后自动恢复（文件存储）
- **双语支持** — 界面和机器人回复均支持中英文

## 部署方式

### 独立模式（Flask）

适用于单机器人部署，运行轻量级 Flask Webhook 服务。

1. 安装：

```bash
pip install lockbot
```

2. 创建机器人脚本（`bot.py`）：

**设备锁机器人**（按 GPU 锁定）：

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

**节点锁机器人**（按节点锁定）：

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

3. 启动：

```bash
python bot.py
```

### 平台模式（FastAPI + 前端）

完整管理平台，包含 Web UI、多机器人编排、用户认证和管理员面板。

1. 安装：

```bash
pip install lockbot
```

2. 设置环境变量：

```bash
export JWT_SECRET="your-jwt-secret"
export ENCRYPTION_KEY="your-fernet-key"    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
export DEV_MODE="true"                      # 开发模式，自动创建管理员用户
```

3. 启动：

```bash
# 后端
uvicorn lockbot.backend.app.main:app --host 0.0.0.0 --port 8000 --reload

# 前端（另一个终端）
cd frontend && npm install && npm run dev
```

### Docker 部署

```bash
docker build -f docker/Dockerfile -t lockbot .

# 平台模式（默认）
docker run -d -p 8000:8000 \
  -e JWT_SECRET=your-secret \
  -e ENCRYPTION_KEY=your-fernet-key \
  -v lockbot-data:/app/python/lockbot/data \
  -v lockbot-state:/data/bots \
  lockbot

# 独立模式（挂载 bot.py，状态持久化到 /data/bots）
docker run -d \
  -v $(pwd)/bot.py:/app/bot.py \
  -v lockbot-state:/data/bots \
  lockbot \
  tmux new-session -d 'python /app/bot.py'
```

## 机器人配置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `BOT_TYPE` | `DEVICE`、`NODE` 或 `QUEUE` | （必填） |
| `BOT_NAME` | 机器人实例名称 | `demo_bot` |
| `CLUSTER_CONFIGS` | 集群布局（字典或列表） | `{}` |
| `TOKEN` | 机器人签名验证 Token | `""` |
| `AESKEY` | 消息解密 AES 密钥 | `""` |
| `WEBHOOK_URL` | 消息发送 Webhook URL | `""` |
| `PORT` | 服务监听端口 | `8090` |
| `DEFAULT_DURATION` | 默认锁定时长（秒） | `7200`（2小时） |
| `MAX_LOCK_DURATION` | 最大锁定时长（秒） | `-1`（不限制） |
| `EARLY_NOTIFY` | 锁定到期前通知 | `false` |

完整配置项参见 `python/lockbot/core/config.py`。

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest

# 代码检查 + 格式化
ruff check python/ tests/
ruff format --check python/ tests/
```

## 许可证

MIT
