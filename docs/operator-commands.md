# LockBot Operator Kick (OpKick) API

## Architecture

```
┌──────────────────┐     GET /api/bots/{id}/locked-users
│  XPU Monitor Bot │────────────────────────────────────> LockBot API
│  (External)      │                                        (query state)
│                  │     POST /api/bots/{id}/opkick
│                  │────────────────────────────────────> LockBot API
└──────────────────┘                                        (release lock + notify user)
```

LockBot only manages locks. Monitoring logic, idle detection, and kick decisions live entirely in the external bot.

## Quick Start

### 1. Generate API Key

In the LockBot Web UI bot detail page, click "Generate". The key is always visible in the detail page. Regenerating invalidates the old key.

### 2. Query Lock State

```bash
curl http://your-lockbot/api/bots/{bot_id}/locked-users \
  -H "Authorization: Bearer lbk_xxxxxxxxxxxxxxxxxxxx"
```

Response (NodeBot/QueueBot):
```json
{
  "bot_id": 1,
  "bot_type": "NODE",
  "nodes": {
    "gpu01": {
      "status": "exclusive",
      "current_users": [
        {"user_id": "zhangsan", "start_time": 1750000000, "duration": 7200}
      ]
    },
    "gpu02": {"status": "idle", "current_users": []}
  }
}
```

### 3. Execute OpKick

```bash
curl -X POST http://your-lockbot/api/bots/{bot_id}/opkick \
  -H "Authorization: Bearer lbk_xxxxxxxxxxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"target_user": "zhangsan", "node_key": "gpu01", "reason": "XPU idle > 10min"}'
```

Parameters:

| Field | Required | Description |
|-------|----------|-------------|
| `target_user` | Yes | The user_id to kick |
| `node_key` | No | Specific node; omit to kick from all nodes |
| `dev` | No | Device IDs (DeviceBot only), e.g. `[0, 2]` |
| `reason` | No | Reason shown in the kick notification to the user |

Response:
```json
{"ok": true, "freed": ["gpu01"]}
```

The kicked user is automatically notified via IM (with reason if provided).

## Rate Limits

| Endpoint | Limit |
|----------|-------|
| `GET /locked-users` | 60/min per IP |
| `POST /opkick` | 30/min per IP |

## API Key Management

| Action | Method |
|--------|--------|
| Generate/Reset | Web UI bot detail page or `POST /api/bots/{id}/api-key` |
| Revoke | Web UI or `DELETE /api/bots/{id}/api-key` |
| Usage | `Authorization: Bearer lbk_xxx` |

Keys are stored in plaintext and always visible in the bot detail page. Regenerating invalidates the old key.

---

# LockBot 运维释放 (OpKick) API

## 架构

```
┌──────────────────┐     GET /api/bots/{id}/locked-users
│  XPU 监控机器人   │────────────────────────────────────> LockBot API
│  (外部)          │                                        (查询状态)
│                  │     POST /api/bots/{id}/opkick
│                  │────────────────────────────────────> LockBot API
└──────────────────┘                                        (释放锁 + 通知用户)
```

LockBot 只管锁。监控逻辑、空闲检测、踢人决策全在外部机器人里。

## 快速开始

### 1. 生成 API Key

在 Web UI 机器人详情页点击"生成"。Key 始终可在详情页查看。重新生成会使旧 Key 失效。

### 2. 查询锁状态

```bash
curl http://your-lockbot/api/bots/{bot_id}/locked-users \
  -H "Authorization: Bearer lbk_xxxxxxxxxxxxxxxxxxxx"
```

### 3. 执行 OpKick

```bash
curl -X POST http://your-lockbot/api/bots/{bot_id}/opkick \
  -H "Authorization: Bearer lbk_xxxxxxxxxxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"target_user": "zhangsan", "node_key": "gpu01", "reason": "XPU空闲超过10分钟"}'
```

参数：

| 字段 | 必填 | 说明 |
|------|------|------|
| `target_user` | 是 | 要释放的用户 ID |
| `node_key` | 否 | 指定节点，不填则释放所有节点 |
| `dev` | 否 | 设备 ID 列表（仅 DeviceBot），如 `[0, 2]` |
| `reason` | 否 | 释放原因，会显示在用户收到的通知中 |

响应：
```json
{"ok": true, "freed": ["gpu01"]}
```

被踢用户会自动收到 IM 通知（包含原因）。

## 频率限制

| 接口 | 限制 |
|------|------|
| `GET /locked-users` | 60次/分钟/IP |
| `POST /opkick` | 30次/分钟/IP |

## API Key 管理

| 操作 | 方式 |
|------|------|
| 生成/重置 | Web UI 机器人详情页 或 `POST /api/bots/{id}/api-key` |
| 撤销 | Web UI 或 `DELETE /api/bots/{id}/api-key` |
| 使用 | `Authorization: Bearer lbk_xxx` |

Key 明文存储，可随时在详情页查看。重新生成会使旧 Key 失效。
