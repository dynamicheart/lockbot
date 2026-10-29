# lockbot

集群资源管理机器人，支持通过即时通讯平台（如百度如流）的对话命令锁定和释放 GPU 设备、集群节点和队列资源。

统一使用 FastAPI + Vue.js 管理平台部署，支持管理一个或多个机器人。

[English](README.md) | [在线演示](https://dynamicheart.github.io/lockbot/)

[![PyPI version](https://img.shields.io/pypi/v/lockbot?color=blue)](https://pypi.org/project/lockbot/)
[![Docker Image](https://img.shields.io/badge/ghcr.io-dynamicheart%2Flockbot-blue?logo=docker)](https://github.com/DynamicHeart/lockbot/pkgs/container/lockbot)

<img src="./docs/demo.gif" width="600" />

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
  -v lockbot-data:/data \
  ghcr.io/dynamicheart/lockbot:latest

# 4. 创建 super_admin（密码自动生成并打印）
docker exec -it lockbot python tools/create_super_admin.py --username admin --email admin@example.com
```

> **数据持久化**：所有数据（SQLite 数据库、机器人状态文件）统一存储在 `/data` 目录下，可通过 `DATA_DIR` 环境变量自定义。

## 机器人配置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `BOT_TYPE` | `DEVICE`、`NODE` 或 `QUEUE` | （必填） |
| `BOT_NAME` | 机器人实例名称 | `demo_bot` |
| `CLUSTER_CONFIGS` | 集群布局（字典或列表） | `{}` |
| `TOKEN` | 机器人签名验证 Token | `""` |
| `AESKEY` | 消息解密 AES 密钥 | `""` |
| `WEBHOOK_URL` | 消息发送 Webhook URL | `""` |
| `DEFAULT_DURATION` | 默认锁定时长（秒） | `7200`（2小时） |
| `MAX_LOCK_DURATION` | 最大锁定时长（秒） | `-1`（不限制） |
| `EARLY_NOTIFY` | 锁定到期前通知 | `false` |
| `FORBID_RELOCK` | 按机器人开启后禁止续期及延长预约领取时长 | `false`（默认关闭） |
| `MAX_RESOURCES_PER_USER` | 每用户每 bot 的占用与预约总量：NODE/QUEUE 按机器，DEVICE/QUEUE_DEVICE 按卡 | `-1`（不限） |

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

### 调试启动

```bash
# 安装依赖
pip install -e ".[dev]"
cd frontend && npm install && cd ..

# 启动后端（端口 8002，支持热重载）
bash scripts/dev/backend.sh

# 启动前端（端口 8001，/api/* 代理到 localhost:8002）
bash scripts/dev/frontend.sh
```

前后端均支持热重载，修改代码后自动生效。

### 测试

```bash
# 运行测试
pytest

# 代码检查 + 格式化
ruff check python/ tests/
ruff format --check python/ tests/
```

## 从旧版独立部署迁移

已删除废弃的 Flask 入口 `lockbot.core.entry.create_app` / `run_bot` 和进程级
`Config.get/set/load_from_file/load_from_env` 接口。构造机器人不再自动启动私有调度器。
统一使用上面的 FastAPI 平台部署，只有一个机器人时也采用同一启动方式。
已有平台 Docker 部署的启动命令、端口和持久化布局保持不变，不需要重建数据库。

旧 standalone 用户需要迁移：

1. 停止旧 Webhook 服务前，保留原配置和 JSON 状态备份。
2. 启动平台并创建对应类型的机器人，在编辑页填写原集群、消息凭据和运行规则。
   不要把凭据写进公开日志。
3. 机器人停止时，通过已有集群状态导入功能恢复原状态；核对资源名、持有者、
   到期时间和队列后再启动。不要覆盖其它机器人的目录，也不要删除旧数据卷。
4. 将消息平台的回调切换为 `/api/bots/webhook/{bot_id}`，启动并测试回调和到期释放。
   验证完成前保留旧镜像和备份。

机器人核心类仍可独立构造用于测试，使用各自的 `Config` 实例；平台调度、状态格式和
旧状态迁移继续保留。独立利用率采集和 Prometheus 部署请参阅
[资源采集器](docs/usage/resource-exporter.md)。

## 许可证

MIT
