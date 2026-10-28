# 飞书消息渲染联调

这是开发用的消息画廊，不是正式飞书适配器。它在临时目录构造虚构资源，用现有 NODE、DEVICE、QUEUE、QUEUE_DEVICE 核心生成帮助、查询、申请、预约及释放回复，再将回复中的 TEXT、LINK、AT 结构转换为飞书 `post` 消息。所有资源修改都发生在本地临时状态，发送端只调用飞书消息 API。

## 验证范围

- Unicode emoji、中文／英文、换行、空行、长帮助消息。
- 原生链接节点，包含内网裸 IP、端口、Skill 路径和 GitHub 链接。
- 真实用户提及与全体提及。虚构 alice/bob 显示为普通文本，不假造平台身份。
- Node、Device、Queue、QueueDevice 的帮助、锁定、释放、排队与查询。
- 轮到自己、可用、到期、自动释放、预约失效、运维释放、绑定及解绑、规则错误、别名、Agent 操作提示。
- 普通文本中的 `<at ...>` 和 Markdown 保持文字，不被当作任意提及或链接解析。

使用富文本行节点保留换行，空行用空白文本节点表示；链接用 `a`，提及用 `at`。当前画廊从如流结构读取是为了核对既有消息，不要求正式飞书适配器依赖如流协议：正式实现仍应直接接受共同的正文、链接和提及语义。

## 运行

在仓库根目录，配置 Python 环境并设置 `PYTHONPATH=python`：

```sh
python scripts/dev/feishu_message_gallery.py --report /tmp/feishu-gallery.json
```

默认只生成本地报告，不发送消息。真实发送需要一个仅本机可读的配置文件：

```json
{
  "chat_id": "oc_TEST_CHAT_ID",
  "tenant_access_token": "APPLICATION_ACCESS_TOKEN",
  "mention_id": "ou_SELECTED_TEST_USER"
}
```

配置文件应放在仓库之外，权限 `0600`。不要将应用密钥或令牌写入命令行、Git 或群聊。令牌从同一企业应用获取，测试用户 ID 也必须属于该应用范围；不要通过姓名猜测 ID。仅向明确选定且授权的测试群发送。

```sh
python scripts/dev/feishu_message_gallery.py \
  --target-file /private/path/target.json \
  --report /private/path/delivery.json --send
```

脚本每条间隔至少 0.6 秒，并为每个批次／样例生成固定请求 UUID。遇到失败或不确定结果立即停止，不自动重试；核对报告后可以通过 `--resume` 复用同一报告及 UUID，并跳过已接受的消息。恢复必须仍在飞书消息去重有效期内，且使用原测试应用和群；不要拿旧报告向另一个群恢复。`--only CASE_NAME` 可单独复测某条。

报告中的 `accepted` 只说明平台 API 接受，不等于客户端已读或视觉验收完成。报告可能包含平台用户 ID，因此只本地保留，不提交公开仓库。

## 2026-10-29 测试记录

已在授权测试企业的既有机器人与指定测试群真实发送主批次 **55 条**，得到 55 个不同消息 ID，包括真实 `@用户` 与 `@全体`。客户端已观察到消息到达，长帮助、emoji 和内网链接被渲染为对应文字与链接元素。姓名查询接口以既有基本信息权限成功返回测试用户姓名，未新增权限、获取手机号或邮箱。

Device 的第一轮测试节点名使用了旧 DEVICE 正则不支持的连字符，因而申请／释放样例显示格式错误；已改用兼容的虚构节点名，补发正确申请、查询和释放样例。未修改生产命令正则。测试环境帮助页脚显示本地已安装包版本，不能用它判断远端部署版本。

客户端截图／滚动自动化出现系统采集错误，最后的真实提及、链接点击跳转及所有空行的逐项视觉核对仍需继续；不能据此写成全部端到端通过。后续反馈应注明样例编号。

接收侧另有遗留：现有应用订阅指向旧 ngrok HTTP 地址，测试 @ 事件在开发者后台显示推送失败。本画廊走出站发送 API，不证明群命令、绑定、提醒调度或长连接接收已接通。正式接入需独立完成事件接收、身份／群隔离及如流回归测试。

参考：[飞书发送消息结构](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json)、[发送消息 API](https://open.feishu.cn/document/server-docs/im-v1/message/create)。
