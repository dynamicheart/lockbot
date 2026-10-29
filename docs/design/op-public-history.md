# OP 公开操作历史与本地分析（第一阶段）

本阶段落实 Issue #21 的只读历史接入与可解释示例。它依赖私密任务权限底座和有界文件读取器，不提供完整资源占用账本，不自动发送群消息，也不关闭 Issue #21 的后续生命周期范围。

## 接口与权限

`GET /api/bots/{bot_id}/op/history` 使用该 bot 专用 OP key。`/api/op-skill` 的 `public_agent_operation_history` 能力和 `op_history_path_template` 用于发现。服务端在读取前、返回前分别刷新 bot 并校验 key，轮转或撤销期间失效即拒绝；个人 Agent key、后台 JWT 和其他 bot 的 OP key 不通用。生成 key 的人即使是超级管理员，OP 响应仍固定使用 `read_history(private=False)`。

这是一个只读接口；既有 OP key 本身仍同时拥有已授权的强制释放能力，没有新增独立的只读凭证类型。历史读取不改变锁、队列、运行状态，也不要求机器人进程正在运行。

当前文件与查询按本部署的 `bot_id` 归属，正常软删除不复用 ID。历史导入或恢复不得把其他机器人/实例的文件放进相同 bot 目录；本阶段不声称提供跨导入实例隔离。若后续支持硬删除、ID 复用或跨实例迁移，需要先单独设计 UUID 与旧文件归属校验。

| 参数 / 字段 | 含义 |
| --- | --- |
| `limit` | 默认 25，范围 1–100 |
| `cursor` | 服务端选定文件的 inode/偏移游标，最大 256 字符，不能作为路径 |
| `query` | 最多 128 字符，在公开投影之后匹配，不能用私密备注、Agent 名或凭证标识命中 |
| `items` | 公开用户身份、时间、固定操作词、请求资源、预设分类、处理结果；不是成功占用清单 |
| `previous_task_category` | 用途修改/删除时可获得的前一个预设分类，旧事件没有时不猜测、不回填 |
| `next_cursor` | 非空则继续；有界扫描或筛选后可以出现空页但仍有后续 |
| `scanned_bytes` | 当前请求实际扫描量，最多 1 MiB |
| `retention` | 当前 10 个、每个最多 5 MiB 的轮转上限，不承诺覆盖某个日期范围 |
| `coverage` | 来源为 `agent_api_operations`；`complete_history`、`complete_lifecycle` 固定为 false |

响应 `Cache-Control: no-store`。每个客户端地址限制 30 次/分钟，建议分页间隔至少 2.1 秒，429 遵守 `Retry-After`。无效游标返回 400；游标文件已被清理返回 409，必须重新开始报告，不能把保留缺口静默当成零活动。不可读取的文件返回 503；损坏行、未完成行和不匹配当前 bot 的记录跳过，不能据此宣称记录完整。

同一共享读取器先限制文件、行长和扫描预算，再按事件字段白名单处理、调用 `public_event`、匹配公开搜索条件，最后分页。普通 OP 不接收私密用途、Agent 名、授权备注、凭证标识、回复正文、持有证明或未知未来字段。原始文件仍受宿主机文件权限管理；应用 RBAC 不构成针对 OS/root 或平台签名凭据持有者的加密隔离。

## 本地示例

`scripts/op_history/report.py` 仅使用 Python 3 标准库，不依赖 Lockbot 包、MPI、Prometheus、SSH 或采集器。无网络的示例：

```sh
python3 scripts/op_history/report.py \
  --input scripts/op_history/sample.jsonl \
  --since 2026-01-01T00:00:00Z --until 2026-01-02T00:00:00Z
```

真实读取时，先通过私有凭证存储向进程注入 `LOCKBOT_OP_KEY`，不要把 key 写进脚本、URL、命令行参数或报告。然后显式指定信任的服务和 bot：

```sh
python3 scripts/op_history/report.py \
  --base-url https://lockbot.example.invalid --bot-id 1 \
  --since 2026-01-01T00:00:00Z --until 2026-01-02T00:00:00Z \
  --max-pages 20 --page-size 100
```

脚本拒绝带用户名/密码、query、fragment 的服务地址，不跟随重定向转发凭证；每页响应最多读取 2 MiB，默认最多 20 页、间隔 2.1 秒。遇到轮转、限流、网络错误或分页预算耗尽时保留已读公共记录、输出 `collection_status: partial`，以退出码 2 表示不完整，不自动重试。即使读完所有可见文件，`complete_history` 仍为 false；请求窗口可能早于保留范围、期间可能存在写入失败。

窗口要求显式时区，左闭右开。示例按 `platform + external_user_id` 聚合同一人的多个 Agent，不按名称识别租约。只按同一个事件 ID 去掉重复分页记录；不同事件 ID 的重试仍计为独立请求，不能靠同命令去重。用户排序按身份，不自动生成用量排行榜。

示例数据中 alice 的 6 条记录包括 3 个 lock 请求、1 个 book 请求和 2 个用途事件；其中有 1 次被拒绝。它不表示锁到了 3 次、一直占用了资源，或比别人使用得更多。报告中 `successful_acquisitions`、`allocated_card_hours`、`measured_utilization` 明确为 null。

`dispatched` 只表示请求被派发，不能证明申请成功；`applied` 表示用途事务提交，不能证明硬件工作；`rejected` 和 `may_have_executed` 分别表示被拒绝、可能已执行但结果不确定。book 是预约请求，不是已获得资源。报告仅在本机输出 JSON，不会自动通知或 @ 用户；发布到群必须另外获得目标及实际内容的授权。

## 后续仍需独立设计

完整 card-hours 必须关联群消息、Agent、OP、获得/续期/释放/到期、排队领取与异常恢复的生命周期，还要识别同一租约与重试、缺失事件。当前 JSONL 只有 Agent API 事件，不能重建全部占用。硬件利用率来自独立指标数据源，不能由历史请求数推算。真实群内报告仍需单独验证目标与渲染；本阶段没有发送测试消息或实现自动排行榜。
