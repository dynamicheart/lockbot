# XPU Monitor Bot Example

An external monitoring bot that queries XPU/GPU utilization and integrates with LockBot's operator command system to automatically release idle resources when the cluster is saturated.

## Kick Strategy (Pessimistic / Reactive)

Like a full EV charging station — only intervene when resources are scarce:

```
┌────────────────────────────────────────────────────────────────────┐
│ Every N min: Query LockBot API (cheap HTTP, no SSH)                │
│              ↓                                                     │
│ idle_nodes > saturation_threshold?                                 │
│   YES → Do nothing. Resources available, no one needs to be kicked │
│   NO  → Cluster saturated, proceed ↓                              │
│              ↓                                                     │
│ SSH to occupied nodes only → check XPU utilization                 │
│              ↓                                                     │
│ User idle (< 5% util)?                                            │
│   First time → Notify: "XPU idle, {grace_minutes}min grace"       │
│   After grace → Re-check, still idle → opkick                     │
└────────────────────────────────────────────────────────────────────┘
```

Key design decisions:
- **No periodic polling of XPU** — SSH is expensive, only triggered when cluster is full
- **Notify before kick** — user gets a warning and grace period to resume work
- **Saturation-gated** — if resources are available, idle users are left alone
- **LockBot API is the cheap signal** — determines lock occupancy without SSH

## Features

- **Query-only mode**: Report XPU status as markdown tables via IM webhook
- **Full mode**: Pessimistic saturation check + notify + grace + opkick
- **Webhook server**: Respond to IM AT messages with real-time status reports
- **Parallel SSH**: Concurrent node queries for fast reporting

## Architecture

```
┌─────────────────────┐       GET /locked-users (cheap, every 5min)
│  XPU Monitor Bot    │──────────────────────> LockBot API
│                     │
│                     │       SSH xpu-smi (only when saturated)
│                     │──────────────────────> GPU/XPU Nodes
│                     │
│                     │       IM: notify idle user
│                     │──────────────────────> IM Webhook
│                     │
│                     │       POST /opkick (after grace)
│                     │──────────────────────> LockBot API
└─────────────────────┘
```

## Setup

1. Copy `config.example.json` to `config.json` and fill in your values:
   - `nodes`: Map each LockBot `node_key` to its physical IP
   - `lockbot_api_key`: Generated from LockBot Web UI
   - `enable_opkick`: Set `false` for query-only mode

2. Ensure SSH key-based access to all nodes (passwordless).

3. Install dependencies:
   ```bash
   pip install requests flask
   ```

## Usage

```bash
# One-shot status report (prints or sends via webhook)
python xpu_monitor_bot.py --report

# Continuous monitoring (pessimistic kick when saturated)
python xpu_monitor_bot.py --loop

# Webhook server (responds to IM AT messages)
python xpu_monitor_bot.py --serve --port 8777
```

## Files

| File | Description |
|------|-------------|
| `xpu_monitor_bot.py` | Main bot: query, report, monitor, opkick |
| `cmd.py` | Parallel SSH executor library |
| `my_xpu_smi.sh` | Standalone XPU query script (uses cmd.py) |
| `config.example.json` | Configuration template |

## Config Reference

| Key | Description |
|-----|-------------|
| `nodes` | `{node_key: {ip, hostname}}` — must match LockBot's node keys |
| `lockbot_api_url` | `GET /api/bots/{id}/locked-users` endpoint |
| `lockbot_api_key` | Bearer token (generated from LockBot UI) |
| `enable_opkick` | `true` = saturation monitor+kick, `false` = query-only |
| `saturation_threshold` | Kick logic triggers only when idle_nodes <= this (default: 1) |
| `grace_minutes` | Warning period before actual kick (default: 5) |
| `check_interval_seconds` | Loop interval (default: 300 = 5min) |
| `infoflow_webhook_url` | IM webhook for sending opkick/notify messages |
| `report_webhook_url` | IM webhook for sending status reports |

## Node Key Mapping

The `nodes` config maps LockBot's logical `node_key` to physical IPs:

```json
{
  "nodes": {
    "gpu01": {"ip": "10.0.0.1", "hostname": "gpu01-a100"},
    "gpu02": {"ip": "10.0.0.2", "hostname": "gpu02-a100"}
  }
}
```

When LockBot's `locked-users` API returns `{"nodes": {"gpu01": ...}}`, the monitor bot knows to SSH to `10.0.0.1` to check XPU utilization for that node.

---

# XPU 监控机器人示例

外部监控机器人，查询 XPU/GPU 利用率并集成 LockBot 运维命令系统，在集群资源紧张时自动释放空闲资源。

## Kick 策略（悲观/被动）

类比电动车充电桩满了的场景——只在资源紧缺时才介入：

```
┌─────────────────────────────────────────────────────────────────┐
│ 每 N 分钟: 查 LockBot API（便宜的 HTTP 请求，不 SSH）          │
│             ↓                                                   │
│ 空闲节点 > saturation_threshold?                                │
│   是 → 什么都不做，资源充裕，没人需要被踢                        │
│   否 → 集群饱和，继续 ↓                                        │
│             ↓                                                   │
│ SSH 查占用中节点的 XPU 利用率（只查占用节点）                    │
│             ↓                                                   │
│ 用户空闲 (< 5%)?                                               │
│   首次 → 通知: "你的 XPU 空闲，{grace_minutes}分钟宽限期"       │
│   宽限后 → 再检查，仍然空闲 → opkick 释放                       │
└─────────────────────────────────────────────────────────────────┘
```

核心设计：
- **不做周期性 XPU 轮询** — SSH 开销大，只在集群满了才触发
- **先通知再踢** — 用户得到警告和宽限期恢复使用
- **饱和门控** — 资源充足时，空闲用户不管
- **LockBot API 是廉价信号** — 只判断锁占用情况，不需要 SSH

## 功能

- **纯查询模式**：以 Markdown 表格报告 XPU 状态
- **完整模式**：悲观饱和检查 + 通知 + 宽限 + opkick
- **Webhook 服务**：响应 IM AT 消息，返回实时状态报告
- **并行 SSH**：并发查询节点，快速出报告

## 使用方式

```bash
# 一次性状态报告
python xpu_monitor_bot.py --report

# 持续监控（集群饱和时才 kick）
python xpu_monitor_bot.py --loop

# Webhook 服务器（响应 IM AT 消息）
python xpu_monitor_bot.py --serve --port 8777
```

## 节点映射

`config.json` 中的 `nodes` 将 LockBot 的逻辑 `node_key` 映射到物理 IP：

```json
{
  "nodes": {
    "gpu01": {"ip": "10.0.0.1", "hostname": "gpu01-a100"},
    "gpu02": {"ip": "10.0.0.2", "hostname": "gpu02-a100"}
  }
}
```

当 LockBot 的 `locked-users` API 返回 `{"nodes": {"gpu01": ...}}` 时，监控机器人知道要 SSH 到 `10.0.0.1` 查询该节点的 XPU 利用率。只有在集群饱和时才会执行 SSH。
