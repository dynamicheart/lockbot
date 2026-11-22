"""Usage notes and executable request examples for the v2 OpenAPI document."""

import json

_RESOURCE = "res_0123456789abcdef0123456789abcdef"
_SECOND_RESOURCE = "res_fedcba9876543210fedcba9876543210"
_BOOKING = "book_0123456789abcdef0123456789abcdef_0123456789abcdef01234567"
_PROOF = "lbp_example_proof"
_GRANT = {"grant_ref": "example_grant_ref", "name": "示例机器人", "platform": "feishu", "chat_id": "oc_example"}
_CARD = {
    "resource_ref": _RESOURCE,
    "grant_ref": _GRANT["grant_ref"],
    "source": _GRANT["name"],
    "platform": _GRANT["platform"],
    "node": "node-23",
    "device": 0,
    "unit": "card",
    "label": None,
    "hardware": [{"model": "p800", "count": 1}],
    "status": "idle",
    "available": False,
    "operations": ["book", "free", "lock"],
    "metrics": "ready",
    "booking": {"booking_ref": _BOOKING, "position": 1, "claimable": True},
}

COMMAND_EXAMPLES = {
    "lock": ("独占锁定一台节点或一张卡", {"operation": "lock", "resource_refs": [_RESOURCE], "duration": "30m"}),
    "lock_multiple": (
        "一次锁定同一个机器人下的多个资源",
        {"operation": "lock", "resource_refs": [_RESOURCE, _SECOND_RESOURCE], "duration": "2h"},
    ),
    "slock": ("加共享锁", {"operation": "slock", "resource_refs": [_RESOURCE], "duration": "30m"}),
    "book": ("排队预约 30 分钟", {"operation": "book", "resource_refs": [_RESOURCE], "duration": "30m"}),
    "lock_booking": ("领取已排到的预约", {"operation": "lock", "booking_ref": _BOOKING}),
    "free": ("释放本人占用或取消本人预约", {"operation": "free", "resource_refs": [_RESOURCE]}),
    "label": ("设置节点标签", {"operation": "label", "resource_refs": [_RESOURCE], "label": "training"}),
    "keep": (
        "确认保留低利用率资源",
        {"operation": "keep", "resource_refs": [_RESOURCE], "notice_ref": "example_notice01"},
    ),
}

_DESCRIPTIONS = {
    ("get", "/manifest"): """
无需凭证。接入新服务时，先读取 `api_version`、`credential_scheme` 和 `openapi_url`。
若 `capabilities` 含 `resource_events`，可通过 `resource_events_url` 订阅资源。
`task_categories` 列出可用于资源操作的预置分类。
`instance_id` 标识当前服务实例，可用它区分不同服务器上的引用。
`skill` 列出服务发布的 Agent Skill 名称、同源下载路径和内容的 SHA-256。下载无需凭证。

```sh
curl -fsS "$LOCKBOT_URL/api/agent/v2/manifest"
```
""",
    ("get", "/skill/SKILL.md"): """
下载当前服务发布的 v2 Skill 正文。无需凭证。安装或更新前，用 `GET /api/agent/v2/manifest`
返回的 `skill.sha256` 核对原始字节。已保存连接的 lb 可以执行 `lb skill install` 和 `lb skill update`。

```sh
curl -fsS "$LOCKBOT_URL/api/agent/v2/skill/SKILL.md"
```
""",
    ("post", "/bindings"): """
请求体可省略，默认 `{"purpose":"bind"}`。首次绑定可以直接发起。已有 v2 Key 时，
用它作为 Bearer 凭证发起 `bind`，可给同一身份增加群授权。
返回 `binding_id`、`bind_code`、`proof` 和 `expires_at`。请用户在目标群向机器人发送
`bind <bind_code>`。客户端保存 `binding_id` 和 `proof`，用于完成绑定。

遗失 Key 时可用 `recover` 并让原帐号在群内重新确认。
`reset` 会在群内确认后更换 Key，使旧 v2 Key 失效。只需找回原 Key 时使用 `recover`。
只在目标群发送 `bind_code`。请私下保存 `proof`，并在 `expires_at` 前完成确认。

```sh
curl -fsS "$LOCKBOT_URL/api/agent/v2/bindings" \\
  -H 'Content-Type: application/json' --data '{"purpose":"bind"}'
```
""",
    ("post", "/bindings/{binding_id}/complete"): """
使用发起绑定返回的 `binding_id` 和 `proof`，提交 JSON `{"proof":"<proof>"}`。
群内确认之前返回 `{"status":"pending"}`。用户确认后再次提交，成功返回
`status: confirmed` 和 `grant`。如果还返回 `credential`，保存为后续调用的 Bearer Key。
带已有 Key 增加群授权时继续使用原 Key。绑定过期或 proof 不匹配时返回 `404 binding_unavailable`。
收到 `pending` 时等用户确认，再用同一个 proof 查询。取得 `credential` 后私下保存。

```sh
jq -nc --arg proof "$PROOF" '{proof:$proof}' |
  curl -fsS "$LOCKBOT_URL/api/agent/v2/bindings/$BINDING_ID/complete" \\
    -H 'Content-Type: application/json' --data-binary @-
```
""",
    ("get", "/bindings"): """
传个人 v2 Key，返回 `grants`，即此 Key 当前可访问的机器人列表。
用 `name`、`platform`、`bot_id` 和 `chat_id` 确认来源。
节点或机器人重名时，取对应的 `grant_ref` 来筛选资源。
`grants: []` 表示当前 Key 没有可用的机器人授权。可以在目标群发起新的绑定。

```sh
curl -fsS "$LOCKBOT_URL/api/agent/v2/bindings" -H "Authorization: Bearer $KEY"
```
""",
    ("delete", "/bindings/{grant_ref}"): """
从 `GET /api/agent/v2/bindings` 取得目标机器人的 `grant_ref`。成功返回 `204`。
同一个 Key 的其他群授权继续有效。释放资源或取消预约请在解除授权前调用 `commands` 的 `free`。
如果需要恢复访问，使用同一 Key 在目标群重新绑定。

```sh
curl -fsS -X DELETE "$LOCKBOT_URL/api/agent/v2/bindings/$GRANT_REF" \\
  -H "Authorization: Bearer $KEY"
```
""",
    ("get", "/resources"): """
- 找可申请的 P800 卡：`?model=p800&unit=card&available=true&limit=20`。
  `model` 大小写等价。查指定卡用 `?node=node-23&device=0`。
- 查自己持有或排队的资源用 `?mine=true`。看等待估算用 `?view=wait`。
  持有人和队列详情可用 `?include_people=true`。
- 节点重名时用 `grant_ref` 限定机器人，或用 `resource_ref` 直接查询一项。
  操作前查看该行的 `operations`。预约排到后，`booking.claimable` 为 true，
  取同一对象的 `booking_ref` 交给 `lock`。
- `next_cursor` 非空时，保持原筛选条件并用 `cursor` 取下一页。
  收到 `409 stale_cursor` 时从第一页重新查。若 `complete` 为 false，
  `unavailable` 为暂时未能读取的机器人授权引用。此时结果可能不完整。

返回的 `device` 是卡号，整节点为 null。`status` 描述当前锁定模式，`available` 表示是否可立即申请。
例如 `status: idle` 的资源可能已留给队首，此时 `available` 为 false。
`holding` 和 `booking` 仅表示调用者本人的占用与预约。
`fields` 可指定逗号分隔的返回字段，`resource_ref` 始终返回。

```sh
curl -fsS "$LOCKBOT_URL/api/agent/v2/resources?model=p800&unit=card&available=true&limit=20" \\
  -H "Authorization: Bearer $KEY"
```

把返回的 `next_cursor` 用 `curl -G --data-urlencode "cursor=$NEXT_CURSOR"` 传回，并保留上页的筛选条件。
""",
    ("get", "/resources/events"): """
用 `Accept: text/event-stream` 和个人 Key 建立 SSE 连接。请将订阅范围控制在 100 个资源以内。
可用 `?mine=true` 关注本人占用与预约，也可用 `node` 或 `resource_ref` 缩小范围。
首个 `snapshot` 事件给出当前状态。随后 `resource.changed` 的 `rows` 是更新的行，
`removed` 是离开当前筛选范围的资源引用。以 `resource_ref` 合并更新。

- `holding.renewal_due` 为 true 时，机器人已经进入到期提醒窗口，`holding.expires_at` 为到期时间。
- `booking.estimated_ready_at` 是预计轮到的时间。收到更晚的时间表示预计等待变长。
- `booking.claimable` 为 true 时可领取，`booking.claim_expires_at` 为领取窗口截止时间（窗口开启后返回）。
- 使用 `mine=true` 时，占用结束或预约离开队列会让该行从视图移除。
  如果同一行还有本人的其他状态，比较更新前后的 `holding` 和 `booking`。
- 在线期间，`ended` 可给出本人曾在此订阅范围内的到期记录。每条含 `resource_ref`、`deadline`
  原因包括 `holding_expired`（锁到期释放）、`holding_reclaimed`（低利用率回收）和 `booking_timed_out`（超时未领取）。
  重连后以当前状态为准。
- `idle.notice` 只发给该 API 锁的持有人。使用其中的 `resource_ref` 和 `notice_ref`
  调用 `POST /commands` 的 `keep`。窗口为 `confirm_within_seconds` 秒，确认后复核时间延后 10 分钟。

心跳以 SSE 注释发送。断线时可携带上次事件的 `Last-Event-ID` 按原条件重连。
如果收到新 `snapshot`，用它替换本地视图。收到 `422 watch_scope_too_large` 时缩小范围。
重连只能恢复当前视图差异。需要保留完整操作历史时，应由客户端自行保存收到的更新。
`error` 事件表示该流即将结束，用其中的 `code` 判断是否需要重新认证或修改筛选条件。

```sh
curl -fsS -N "$LOCKBOT_URL/api/agent/v2/resources/events?mine=true" \\
  -H "Authorization: Bearer $KEY" -H 'Accept: text/event-stream'
```

在终端按 Ctrl+C 停止订阅。连接容量已满时，按 `Retry-After` 等待后重连。
""",
    ("get", "/resources/{resource_ref}/metrics"): """
通过 `GET /api/agent/v2/resources` 找到资源，查看该行的 `metrics` 是否为 `ready`。
个人 v2 Key 需要有目标机器人的授权。资源可见即可查询，无需先锁定。

本接口返回 `source` 和临时只读凭证 `access`。用 `access.token` 作为 Bearer 凭证，
向 `source.endpoint` 下的 `/api/v1/query` 发送表单 POST 查询数据。
使用 `source.node` 和 `source.device` 对应采集端的节点与卡。
`access.expires_at` 为 Unix 秒，到期后再向 LockBot 换取只读凭证。
`404 metrics_not_configured` 表示缺少可用的数据源或映射。

先把 `RESOURCE_REF` 设为查询返回的资源引用，再获取采集端的凭证。

```sh
curl -fsS "$LOCKBOT_URL/api/agent/v2/resources/$RESOURCE_REF/metrics" \\
  -H "Authorization: Bearer $KEY"
```

将 `METRICS_ENDPOINT` 和 `METRICS_TOKEN` 分别设为返回的 `source.endpoint` 和 `access.token`。
查询下面的示例时，将 `node-23` 替换为返回的 `source.node`。节点名中的正则特殊字符需先转义，
再把正则作为 JSON 字符串编码。结果中的 `metric.device` 可与 `source.device` 对照。

```sh
curl -fsS "$METRICS_ENDPOINT/api/v1/query" \\
  -H "Authorization: Bearer $METRICS_TOKEN" \\
  --data-urlencode 'query=lockbot_accelerator_utilization_percent{node=~"^(?:node-23)$"}' \\
  --data-urlencode 'timeout=4s'
```
""",
    ("post", "/commands"): """
使用个人 v2 Key 作为 Bearer 凭证，提交 `application/json`。下方 **Examples** 可以切换请求示例。
示例引用是占位值，请用 `GET /api/agent/v2/resources` 的实际返回值替换。

| 要做什么 | `operation` | 请求字段 |
| --- | --- | --- |
| 独占锁定或续锁 | `lock` | `resource_refs`，可加 `duration` |
| 共享锁定或续锁 | `slock` | `resource_refs`，可加 `duration` |
| 加入排队 | `book` | `resource_refs`，可加 `duration` |
| 轮到自己后开始使用 | `lock` | `booking_ref`，省略 `resource_refs` 和 `duration`，使用预约时长 |
| 释放自己持有的资源或取消自己的预约 | `free` | `resource_refs` |
| 设置或清除节点标签 | `label` | 一个 `resource_refs` 元素和 `label`。空字符串清除标签 |
| 确认保留低利用率资源 | `keep` | 填入 `idle.notice` 的 `notice_ref`，将同一事件的 `resource_ref` 放入 `resource_refs` |

查目标行的 `operations` 确认机器人支持哪些操作。可一次传 1–64 个不重复的资源引用，
全部来自同一个机器人。整节点和卡使用各自查询结果中的引用。
时长单位为 `m`、`h`、`d`，如 `30m`、`1.5h`。省略时按该命令的群规则处理。
`lock`、`slock` 和 `book` 可以带 `task_category`，取值见 `GET /api/agent/v2/manifest`。

成功返回 `status: completed` 及各资源的操作后状态。排队成功表示已登记预约。
`keep` 返回 `review_at`，表示延后的利用率复核时间。
查询 `booking.claimable` 判断何时可以领取。普通 `lock` 或续锁前应确认当前占用与排队状态。

每次逻辑操作可以设置一个新的 `Idempotency-Key`。相同 Key 和请求体重发可读取已有结果；
同一 Key 搭配不同请求体会返回 `409 idempotency_key_reused`。
网络中断或收到 `may_have_executed` 时先查询资源状态，避免重复锁定或续锁。
业务错误可从响应的 `code` 或 `detail.code` 读取。

先从资源查询取 `RESOURCE_REF`。保持 `REQUEST_ID` 对同一次操作不变。

```sh
jq -nc --arg ref "$RESOURCE_REF" '{operation:"lock",resource_refs:[$ref],duration:"30m"}' |
  curl -fsS "$LOCKBOT_URL/api/agent/v2/commands" \\
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \\
    -H "Idempotency-Key: $REQUEST_ID" --data-binary @-
```
""",
}


def _examples(named):
    return {name: {"summary": summary, "value": value} for name, (summary, value) in named.items()}


def _json_examples(operation, status, description, named):
    response = operation["responses"].setdefault(status, {})
    response["description"] = description
    body = response.setdefault("content", {}).setdefault("application/json", {})
    body["examples"] = _examples(named)


def _page(*rows):
    return {"rows": list(rows), "next_cursor": None, "complete": True, "unavailable": [], "unavailable_count": 0}


_JSON_RESPONSES = {
    ("get", "/manifest"): (
        "200",
        "服务标识、协议版本和可用能力。",
        {
            "discovery": (
                "支持 SSE 的 v2 服务",
                {
                    "service_id": "lockbot.resource-api",
                    "instance_id": "0123456789abcdef0123456789abcdef",
                    "api_version": "2",
                    "credential_scheme": "lbv2_personal",
                    "openapi_url": "/api/agent/v2/openapi.json",
                    "capabilities": ["resource_events"],
                    "resource_events_url": "/api/agent/v2/resources/events",
                    "task_categories": {"unit_testing": "单元测试", "performance_testing": "性能测试"},
                    "skill": {
                        "name": "lockbot",
                        "download_url": "/api/agent/v2/skill/SKILL.md",
                        "sha256": "0123456789abcdef" * 4,
                    },
                },
            ),
        },
    ),
    ("post", "/bindings"): (
        "201",
        "已生成待确认绑定。",
        {
            "started": (
                "首次发起绑定",
                {
                    "binding_id": "example_binding_id",
                    "bind_code": "V2-ACDEFGHJ",
                    "proof": _PROOF,
                    "expires_at": "2030-01-01T12:10:00Z",
                },
            ),
        },
    ),
    ("post", "/bindings/{binding_id}/complete"): (
        "200",
        "群内确认状态及绑定结果。",
        {
            "pending": ("等待用户在群内确认", {"status": "pending"}),
            "first_binding": (
                "确认成功并返回个人 Key",
                {
                    "status": "confirmed",
                    "grant": _GRANT,
                    "credential": "lbv2_example_key",
                },
            ),
            "existing_key": ("已有 Key 增加群授权", {"status": "confirmed", "grant": _GRANT}),
        },
    ),
    ("get", "/bindings"): (
        "200",
        "当前个人 Key 的有效群授权。",
        {
            "bound": ("有一个群授权", {"grants": [{**_GRANT, "bot_id": 42}]}),
            "empty": ("尚无有效授权", {"grants": []}),
        },
    ),
    ("get", "/resources"): (
        "200",
        "匹配筛选条件的一页资源，包含分页和读取完整性信息。",
        {
            "available": (
                "可立即申请的卡",
                _page(
                    {
                        **{key: value for key, value in _CARD.items() if key != "booking"},
                        "available": True,
                    }
                ),
            ),
            "ready_booking": ("刚排到的本人预约", _page(_CARD)),
            "partial": (
                "一个机器人暂时无法读取",
                {
                    **_page(),
                    "complete": False,
                    "unavailable": [_GRANT["grant_ref"]],
                    "unavailable_count": 1,
                },
            ),
        },
    ),
    ("get", "/resources/{resource_ref}/metrics"): (
        "200",
        "数据源位置和临时只读凭证。",
        {
            "card": (
                "指定卡的只读凭证",
                {
                    "resource_ref": _RESOURCE,
                    "node": "node-23",
                    "card": 0,
                    "source": {
                        "endpoint": "https://metrics.example.invalid",
                        "node": "node-23",
                        "device": "0",
                        "stale_seconds": 90,
                    },
                    "access": {
                        "token": "example_only",
                        "scope": "metrics:read",
                        "expires_at": 1893499500,
                        "scope_version": 2,
                        "node_ids": ["node-23"],
                    },
                },
            ),
        },
    ),
}

_ERRORS = {
    ("post", "/bindings/{binding_id}/complete"): {
        "404": "binding_unavailable。核对发起时的 ID 和 proof，过期后重新发起绑定。",
        "409": "credential_unavailable。已确认但无法取回 Key。可经原帐号在群内重新确认 reset。",
    },
    ("delete", "/bindings/{grant_ref}"): {
        "404": "binding_unavailable。该授权不存在或不属于此 Key。请刷新授权列表。",
    },
    ("get", "/resources"): {
        "404": "resource_unavailable。精确引用不可见或已失效。重新查询资源。",
        "409": "stale_cursor。清空 cursor，从第一页重新查询。",
        "422": "筛选条件、字段名或分页游标无效。也可能是单个详情行过大，可减少返回字段。",
    },
    ("get", "/resources/events"): {
        "404": "resource_unavailable。检查精确资源引用和群授权。",
        "422": "筛选参数无效，或订阅范围过大。用资源引用、节点或群授权缩小范围。",
    },
    ("get", "/resources/{resource_ref}/metrics"): {
        "404": "resource_unavailable 或 metrics_not_configured。刷新资源列表并检查是否有数据源映射。",
        "503": "metrics_access_unavailable。数据源当前无法签发只读凭证。",
    },
}


def describe_v2_operation(path, method, operation):
    route = path.removeprefix("/api/agent/v2")
    key = (method, route)
    description = _DESCRIPTIONS.get(key)
    if description:
        operation["description"] = description.strip()
    if key in _JSON_RESPONSES:
        _json_examples(operation, *_JSON_RESPONSES[key])
    if route.endswith("/complete"):
        operation["requestBody"]["content"]["application/json"]["examples"] = _examples(
            {
                "proof": ("使用发起绑定时返回的 proof", {"proof": _PROOF}),
            }
        )
    if key == ("delete", "/bindings/{grant_ref}"):
        operation["responses"]["204"]["description"] = "授权已撤销。响应体为空。"
    if key == ("get", "/resources/events"):
        row = {
            name: _CARD[name]
            for name in (
                "resource_ref",
                "grant_ref",
                "source",
                "node",
                "device",
                "unit",
                "hardware",
                "status",
                "available",
                "booking",
            )
        }
        snapshot = json.dumps({"complete": True, "unavailable": [], "rows": [row]}, ensure_ascii=False)
        stream = operation["responses"]["200"]["content"]["text/event-stream"]
        stream["schema"] = {"type": "string"}
        stream["example"] = f"id: 0123456789abcdef0123456789abcdef\nevent: snapshot\ndata: {snapshot}\n\n"
    responses = operation["responses"]
    if key == ("get", "/skill/SKILL.md"):
        responses["200"]["description"] = "完整的 UTF-8 Skill Markdown。"
    if key not in {("get", "/manifest"), ("get", "/skill/SKILL.md"), ("post", "/bindings/{binding_id}/complete")}:
        responses.setdefault("401", {"description": "向 Authorization 头传入有效的 lbv2_ Bearer Key。"})
    for status, guidance in _ERRORS.get(key, {}).items():
        responses.setdefault(status, {})["description"] = guidance
    if key == ("post", "/commands"):
        _describe_commands(operation)


def _describe_commands(operation):
    body = operation["requestBody"]["content"]["application/json"]
    body["examples"] = _examples(COMMAND_EXAMPLES)
    for parameter in operation.get("parameters", ()):
        if parameter["name"].lower() == "idempotency-key":
            parameter["description"] = "同一逻辑操作重试时复用原值。新的操作使用新值。"
    responses = operation["responses"]
    resource = {"resource_ref": _RESOURCE, "node": "node-23", "device": 0}
    responses["200"] = {
        "description": "操作完成。resources 为此次操作后返回的资源状态。",
        "content": {
            "application/json": {
                "examples": {
                    "lock": {
                        "summary": "锁定成功",
                        "value": {
                            "operation": "lock",
                            "status": "completed",
                            "resources": [
                                {
                                    **resource,
                                    "status": "exclusive",
                                    "holding": {"expires_at": "2030-01-01T12:30:00+00:00", "mode": "exclusive"},
                                }
                            ],
                        },
                    },
                    "book": {
                        "summary": "预约登记成功",
                        "value": {
                            "operation": "book",
                            "status": "completed",
                            "resources": [
                                {
                                    **resource,
                                    "status": "exclusive",
                                    "booking_ref": _BOOKING,
                                }
                            ],
                        },
                    },
                    "free": {
                        "summary": "释放成功",
                        "value": {
                            "operation": "free",
                            "status": "completed",
                            "resources": [{**resource, "status": "idle"}],
                        },
                    },
                }
            }
        },
    }
    for status, guidance in {
        "401": "凭证无效或已失效。检查 Bearer Key 是否为有效的 lbv2_ 凭证。",
        "403": "此机器人关闭了用户标签功能（labels_disabled）。",
        "404": "资源不存在、已变化或当前 Key 无权访问（resource_unavailable）。重新查询资源。",
        "409": "操作被规则拒绝（command_rejected），预约不可领取（booking_not_claimable），或幂等请求冲突。"
        "读取 code 和可选的 message。若为 may_have_executed，先查状态。",
        "422": "字段无效、资源跨机器人（cross_bot_batch）或机器人不支持该命令（command_not_supported）。",
        "503": "机器人暂不可用，或执行结果未知。若为 may_have_executed，先查资源状态再决定下一步。",
    }.items():
        responses.setdefault(status, {})["description"] = guidance
