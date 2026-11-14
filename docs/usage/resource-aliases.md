# Agent 读取和验证节点备注

节点 alias 是同一机器人内所有用户可见的公共标签，不是个人任务说明、用途分类或 Agent 名称。详细任务继续使用 `purpose`、`agent_label` 和 usage declarations，不能把私密内容写进 alias。

通过当前机器人的个人 Key 调用 `GET /api/bots/{bot_id}/resources`，读取 `resources[].alias`。未配置或已清空时返回空字符串。NODE、QUEUE 的 alias 位于节点；DEVICE、QUEUE_DEVICE 的 alias 也只位于父节点，不代表某一张卡的独立备注。操作仍使用原始节点 `id`，不把 alias 当作节点标识。

用户已授权修改公共标签时：

1. 读取当前节点，并参考 `/api/agent/context` 的 `bot_rules.allow_user_alias`。实际禁用编辑会返回 `409 aliases_disabled`，现有 alias 仍可读取。
2. 按现有语法发送 `alias node1 标签`；修改使用同一语法，清空使用 `alias node1`。标签最多 15 个字符，不能换行，只接受已配置的节点键。
3. 检查真实命令 `reply`，再读 `/resources` 比较对应节点的 `alias`。HTTP 200 只证明命令已分派，错误目标或过长文本仍可能返回 HTTP 200；不能仅据此报告修改成功。
4. 超时或不确定结果先读回，不自动重放写命令。同一标签是共享配置，读到相同值也不证明一定由当前 Agent 的命令产生。

`captured_at` 和 `age_seconds` 描述锁／队列状态，可能来自短期缓存。alias 每次从当前运行实例配置读取，其读取时刻单独记为 `aliases_observed_at`。这两种观察并非原子快照：新 alias 可以与较早缓存的锁状态同时返回，不能用其中一个时刻替另一个背书。

alias 持久化沿用现有尽力保存行为。命令回复和 `/resources` 读回均不能单独证明数据库已经保存或重启后仍保留；内存更新、持久化失败时，API 仍会如实返回当前运行值。需要持久化验收时，以真实配置保存结果和另行授权的重启验收为准。

`ALLOW_USER_ALIAS` 保留现有编辑开关，`SHOW_NODE_ALIAS` 保留原有 IM 消息显示开关；读回字段不改变如流或飞书消息格式，也不修改锁、队列或凭证权限。旧服务若没有 `alias` 字段，应记为尚不支持结构化读回，不能把缺字段当成已清空。
