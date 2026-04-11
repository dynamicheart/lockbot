# lockbot

集群资源管理机器人，支持通过即时通讯平台（如百度如流）的对话命令锁定和释放 GPU 设备、集群节点和队列资源。

支持两种部署方式：FastAPI + Vue.js 前端的管理平台模式（推荐），以及单机 Flask 独立部署。

[English](README.md) | [在线演示](https://dynamicheart.github.io/lockbot/)

## 功能特性

- **设备锁机器人** — 按单个 GPU/设备维度锁定和释放
- **节点锁机器人** — 按整个集群节点维度锁定和释放
- **队列机器人** — 管理资源分配队列，支持预约和抢占
- **平台模式** — Web 管理界面（Vue 3 + Element Plus），支持多机器人管理、
  用户认证（JWT）、角色权限控制和实时日志
- **状态持久化** — 机器人状态重启后自动恢复（文件存储）
- **双语支持** — 界面和机器人回复均支持中英文

## 快速开始 — 平台模式（推荐）

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

4. 打开 `http://localhost:8000`。

### Docker 部署

```bash
# 1. 生成 ENCRYPTION_KEY 和 JWT_SECRET
python tools/gen_keys.py

# 2. 拉取预构建镜像（或从源码构建）
docker pull ghcr.io/dynamicheart/lockbot:latest
# docker build -f docker/Dockerfile -t lockbot .

# 3. 运行（将密钥替换为生成的值）
docker run -d --name lockbot -p 8000:8000 \
  -e JWT_SECRET=your-secret \
  -e ENCRYPTION_KEY=your-fernet-key \
  -v lockbot-data:/app/python/lockbot/data \
  ghcr.io/dynamicheart/lockbot:latest

# 4. 创建 super_admin（密码自动生成并打印）
docker exec -it lockbot python tools/create_super_admin.py --username admin --email admin@example.com
```

> **数据库**：SQLite 文件自动创建于 `DATA_DIR/lockbot.db`（默认：`python/lockbot/data/lockbot.db`），可通过 `DATA_DIR` 环境变量自定义。

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

## 命令列表

| 命令 | 说明 |
|------|------|
| `lock <node> [时长]` | 独占锁定（如 `lock gpu0 3d`、`lock node1 30m`） |
| `slock <node> [时长]` | 共享锁定（多人可用） |
| `unlock <node>` / `free <node>` | 释放指定节点 |
| `unlock` / `free` | 释放你的所有资源 |
| `kickout <node>` | 强制释放（管理员） |
| `book <node> [时长]` | 队列模式：预约排队 |
| `take <node>` | 队列模式：抢占当前锁定 |
| `<node>` | 查询资源使用情况 |
| `help` | 显示使用指南 |

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

## 独立模式

适用于单机器人部署，运行轻量级 Flask Webhook 服务。

**设备锁机器人**（按 GPU 锁定）：

```python
from lockbot.core.bot_instance import BotInstance
from lockbot.core.entry import create_app

instance = BotInstance("DEVICE", {
    "BOT_NAME": "my-gpu-bot",
    "WEBHOOK_URL": "https://your-webhook-url",
    "TOKEN": "your-bot-token",
    "AESKEY": "your-aes-key",
    "CLUSTER_CONFIGS": {
        "node0": ["A800", "A800", "H100"],
        "node1": ["A800", "H100"],
    },
})

app = create_app(bot=instance.bot, bot_name="my-gpu-bot", port=8000)
app.run(host="0.0.0.0", port=8000)
```

**节点锁 / 排队机器人**（按节点锁定或排队调度）：

```python
from lockbot.core.bot_instance import BotInstance
from lockbot.core.entry import create_app

instance = BotInstance("NODE", {       # 改为 "QUEUE" 即为排队模式
    "BOT_NAME": "my-node-bot",
    "WEBHOOK_URL": "https://your-webhook-url",
    "TOKEN": "your-bot-token",
    "AESKEY": "your-aes-key",
    "CLUSTER_CONFIGS": ["node0", "node1", "node2", "node3"],
})

app = create_app(bot=instance.bot, bot_name="my-node-bot", port=8000)
app.run(host="0.0.0.0", port=8000)
```

## 许可证

MIT
