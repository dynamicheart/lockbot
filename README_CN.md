# lockbot

支持如流 / Infoflow、飞书 / Feishu 的集群资源管理与排队机器人，通过群聊命令锁定、预约和释放节点或设备。

统一使用 FastAPI + Vue.js 管理平台部署，支持管理一个或多个机器人。

[English](README.md) | [在线演示](https://dynamicheart.github.io/lockbot/)

[![PyPI version](https://img.shields.io/pypi/v/lockbot?color=blue)](https://pypi.org/project/lockbot/)
[![Docker Image](https://img.shields.io/badge/ghcr.io-dynamicheart%2Flockbot-blue?logo=docker)](https://github.com/DynamicHeart/lockbot/pkgs/container/lockbot)

<img src="./docs/demo.gif" width="600" />

## 功能特性

- **设备锁机器人** — 按单个 GPU/设备维度锁定和释放
- **节点锁机器人** — 按整个集群节点维度锁定和释放
- **队列机器人** — 管理资源分配队列，支持预约和抢占
- **按卡排队机器人** — 使用 `QUEUE_DEVICE` 按设备预约和排队
- **Agent 接入** — 个人授权、资源发现和版本化 Skill
- **利用率** — 浏览器和 Agent 可选直连独立 Prometheus 数据源
- **平台模式** — Web 管理界面（Vue 3 + Element Plus），支持多机器人管理、
  用户认证（JWT）、角色权限控制和实时日志
- **状态持久化** — 机器人状态重启后自动恢复（文件存储）
- **双语支持** — 界面和机器人回复均支持中英文

## 快速开始 — Docker

正式镜像包含网页界面和飞书依赖。按照[部署指南](docs/agents/deployment/AGENTS.md)准备私有环境文件，配置 `JWT_SECRET` 和 `ENCRYPTION_KEY`。升级时保留原密钥和数据卷。

```bash
docker pull ghcr.io/dynamicheart/lockbot:v3.0.0
docker run -d --name lockbot -p 8000:8000 \
  --env-file /private/path/lockbot.env \
  -v lockbot-data:/data \
  ghcr.io/dynamicheart/lockbot:v3.0.0

# 仅新安装：在交互终端创建初始超级管理员。
docker exec -it lockbot python tools/create_super_admin.py --username admin --email admin@example.com
```

打开 `http://localhost:8000`。数据保存在 `/data`；升级沿用原端口和挂载。同一数据目录只运行一个进程实例。

- [Agent 辅助部署与升级](docs/agents/deployment/AGENTS.md)：环境检查和 Compose 模板
- [如流与飞书配置](docs/agents/platforms/AGENTS.md)、[飞书图文指南](docs/usage/feishu-setup.md)
- [Agent API 与 Skill](docs/agent-api/SKILL.md)
- [独立利用率服务部署](docs/usage/telemetry-deployment.md)、[网页利用率](docs/usage/browser-utilization.md)

飞书采用一个企业应用服务多个资源群，每群对应独立的资源机器人，不支持单聊操作。如流与飞书的凭证和用户独立，复制配置不会共享实时锁或授权。

## 机器人配置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `BOT_TYPE` | `NODE`、`DEVICE`、`QUEUE` 或 `QUEUE_DEVICE` | （必填） |
| `BOT_NAME` | 机器人实例名称 | `demo_bot` |
| `CLUSTER_CONFIGS` | 集群布局（字典或列表） | `{}` |
| `TOKEN` | 如流签名验证 Token | `""` |
| `AESKEY` | 如流消息解密 AES 密钥 | `""` |
| `WEBHOOK_URL` | 如流消息发送 Webhook URL | `""` |
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
pip install -e ".[dev,feishu]"
cd frontend && npm install && cd ..

# 启动后端（端口 8002，支持热重载）
bash scripts/dev/backend.sh

# 启动前端（端口 8001，/api/* 代理到 localhost:8002）
bash scripts/dev/frontend.sh
```

开发页面位于 `http://localhost:8001`，API 代理到 8002。热重载脚本用于开发环境，正式部署使用上面的镜像；后续开发基于 `main`。

### 测试

```bash
# 按文件隔离的并发测试
pytest -n auto --maxprocesses=4 --dist=loadfile

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
