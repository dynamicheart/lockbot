"""Copyable HTTP examples for the public Agent API documentation pages."""

from html import escape

_SETUP = """ORIGIN='https://lockbot.example.com'
umask 077
AUTH_DIR="$(mktemp -d)"
"""
_V2_BIND = (
    _SETUP
    + """API="$ORIGIN/api/agent/v2"
curl -fsS -H 'Content-Type: application/json' -d '{}' "$API/bindings" > "$AUTH_DIR/binding.json"
jq '{binding_id,bind_code}' "$AUTH_DIR/binding.json"
"""
)
_V2_CONFIRM = r"""BINDING_ID="$(jq -er .binding_id "$AUTH_DIR/binding.json")"
jq -c '{proof}' "$AUTH_DIR/binding.json" |
  curl -fsS -H 'Content-Type: application/json' --data-binary @- \
    "$API/bindings/$BINDING_ID/complete" > "$AUTH_DIR/confirmed.json"
jq -er 'select(.status == "confirmed") | "Authorization: Bearer " + .credential' \
  "$AUTH_DIR/confirmed.json" > "$AUTH_DIR/header"
"""
_V2_QUERY = r"""curl -fsS -H @"$AUTH_DIR/header" "$API/bindings"
curl -fsS -H @"$AUTH_DIR/header" \
  "$API/resources?unit=card&model=P800&available=true&limit=5" > "$AUTH_DIR/resources.json"
jq '.rows[] | {node,device,source,resource_ref,operations}' "$AUTH_DIR/resources.json"
"""
_V2_LOCK = r"""REF='res_replace_with_the_selected_resource_ref'
REQUEST_ID="$(uuidgen)"
jq -nc --arg ref "$REF" '{operation:"lock",resource_refs:[$ref],duration:"30m"}' |
  curl -fsS -H @"$AUTH_DIR/header" -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $REQUEST_ID" --data-binary @- "$API/commands"
"""
_V2_BOOK = r"""REF='res_replace_with_a_queueable_resource_ref'
jq -nc --arg ref "$REF" '{operation:"book",resource_refs:[$ref],duration:"30m"}' |
  curl -fsS -H @"$AUTH_DIR/header" -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $(uuidgen)" --data-binary @- "$API/commands"

curl -fsS -H @"$AUTH_DIR/header" "$API/resources?mine=true" |
  jq '.rows[] | select(.booking != null) | {node,device,booking}'

BOOKING_REF='book_replace_with_the_ready_booking_ref'
jq -nc --arg ref "$BOOKING_REF" '{operation:"lock",booking_ref:$ref}' |
  curl -fsS -H @"$AUTH_DIR/header" -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $(uuidgen)" --data-binary @- "$API/commands"
"""
_V2_FREE = r"""jq -nc --arg ref "$REF" '{operation:"free",resource_refs:[$ref]}' |
  curl -fsS -H @"$AUTH_DIR/header" -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $(uuidgen)" --data-binary @- "$API/commands"
"""
_V1_QUERY = r"""curl -fsS -H @"$AUTH_DIR/header" "$ORIGIN/api/agent/context" > "$AUTH_DIR/context.json"
RESOURCES="$(jq -er .links.resources "$AUTH_DIR/context.json")"
COMMANDS="$(jq -er .links.commands "$AUTH_DIR/context.json")"
curl -fsS -H @"$AUTH_DIR/header" "$ORIGIN$RESOURCES"
"""
_V1_CHECK = (
    _SETUP
    + r"""printf 'v1 Key: '
IFS= read -rs KEY
printf '\n'
printf 'Authorization: Bearer %s\n' "$KEY" > "$AUTH_DIR/header"
unset KEY

"""
    + _V1_QUERY
)
_V1_BIND = (
    _SETUP
    + r"""curl -fsS -H 'Content-Type: application/json' -d '{}' \
  "$ORIGIN/api/resource-auth/bindings" > "$AUTH_DIR/binding.json"
jq '{binding_id,bind_code}' "$AUTH_DIR/binding.json"

# Continue after the user confirms the code in the target chat.
BINDING_ID="$(jq -er .binding_id "$AUTH_DIR/binding.json")"
curl -fsS "$ORIGIN/api/resource-auth/bindings/$BINDING_ID"
jq -er '"Authorization: Bearer " + .credential' "$AUTH_DIR/binding.json" > "$AUTH_DIR/header"

"""
    + _V1_QUERY
)
_V1_COMMANDS = r"""curl -fsS -H @"$AUTH_DIR/header" -H 'Content-Type: application/json' \
  -d '{"command":"lock node-23 dev0 30m"}' "$ORIGIN$COMMANDS"
curl -fsS -H @"$AUTH_DIR/header" -H 'Content-Type: application/json' \
  -d '{"command":"free node-23 dev0"}' "$ORIGIN$COMMANDS"
"""

_V2_STEPS = (
    (
        "1. 发起绑定",
        "1. Start binding",
        "将 ORIGIN 换为你的服务地址。首次请求发送空 JSON。本人在目标群 @ 机器人，发送 bind 加返回的 bind_code。",
        "Set ORIGIN to your service. Start with an empty JSON body. "
        "In the target chat, mention the bot and send bind followed by the returned bind_code.",
        _V2_BIND,
    ),
    (
        "2. 确认并保存个人 Key",
        "2. Confirm and save your Key",
        "群内确认后，用原请求的 proof 完成绑定。响应为 pending 时等待确认。Swagger 的 Authorize 中填完整 lbv2_ Key。",
        "After chat confirmation, complete the original request with its proof. "
        "Wait for confirmation if the status is pending. Enter the full lbv2_ Key in Swagger Authorize.",
        _V2_CONFIRM,
    ),
    (
        "3. 查可申请的资源",
        "3. Find an available resource",
        "先看已授权的机器人，再选资源。筛整机用 unit=node，筛卡用 unit=card。存在 next_cursor 时可沿用条件翻页。",
        "Check your authorized bots, then select a resource. Use unit=node for whole nodes or unit=card for cards. "
        "Pass next_cursor as cursor with the same filters to continue.",
        _V2_QUERY,
    ),
    (
        "4. 锁定一项资源",
        "4. Lock a resource",
        "把 REF 换成你选中的 resource_ref。整机和卡用同一请求。"
        "请求结果不明时先用 mine=true 核对占用。重试同一个操作时保留原请求体和原 Idempotency-Key。",
        "Replace REF with the selected resource_ref. Nodes and cards use the same request. "
        "If the outcome is uncertain, check mine=true. Keep the original body and Idempotency-Key for a retry.",
        _V2_LOCK,
    ),
    (
        "5. 排队并在轮到时锁定",
        "5. Book and lock when ready",
        "另选一项支持 book 的资源预约。mine=true 返回 booking。"
        "claimable 为 true 时，用该 booking_ref 执行 lock，时长取自原预约。",
        "Choose a resource that supports book. Read its booking using mine=true. "
        "When claimable is true, use its booking_ref with lock. The booked duration applies.",
        _V2_BOOK,
    ),
    (
        "6. 释放或取消排队",
        "6. Release or cancel a booking",
        "用选定资源的 REF 释放你自己的锁，或取消你自己的预约。",
        "Use the selected REF to release your holding or cancel your booking.",
        _V2_FREE,
    ),
)

_V1_STEPS = (
    (
        "1. 用已有 Key 查资源",
        "1. Query with an existing Key",
        "输入已有的 lbr_ Key。一个 Key 对应一个机器人。先查 context，再按返回的链接查询资源。",
        "Enter your existing lbr_ Key. Each Key is scoped to one bot. "
        "Read context and follow the returned resource link.",
        _V1_CHECK,
    ),
    (
        "2. 首次绑定",
        "2. First-time binding",
        "仅在仍开放 v1 新绑定的服务上执行。本人在目标群 @ 机器人，发送 bind 加返回的 bind_code。"
        "状态为 confirmed 后使用发起请求返回的 Key。",
        "Use this on services that still allow v1 enrollment. In the target chat, mention the bot and send bind "
        "followed by the returned code. Use the original response's Key once the status is confirmed.",
        _V1_BIND,
    ),
    (
        "3. 锁定、排队与释放",
        "3. Lock, book and release",
        "用查询返回的节点名和卡号，整机省略 dev0。用 book 排队，轮到时执行资源响应给出的 claim_command。",
        "Use the node and device from the resource query. Omit dev0 for a whole node. "
        "Use book to queue and run the returned claim_command when ready.",
        _V1_COMMANDS,
    ),
)

TUTORIAL_STYLE = """
<style>
  .agent-tutorial { margin: 1.25rem auto; padding: 0 1.25rem; max-width: 1160px;
    color: #263042; font: 15px/1.55 system-ui, sans-serif; }
  .agent-tutorial h2 { margin-bottom: .4rem; }
  .agent-tutorial details { padding: .75rem 0; border-bottom: 1px solid #d9e1ed; }
  .agent-tutorial summary { cursor: pointer; font-weight: 600; }
  .agent-tutorial pre { overflow-x: auto; padding: .8rem; background: #f2f5fa; }
</style>
"""


def agent_tutorial(version: int, lang: str, *, reference_only=False) -> str:
    english = lang == "en"
    title = "HTTP quickstart" if english else "HTTP 快速开始"
    intro = (
        (
            "Use curl and jq. This v1 guide applies to deployments that still expose the v1 API."
            if english
            else "示例使用 curl 和 jq，适用于仍提供 Agent API v1 的 LockBot 服务。"
        )
        if version == 1
        else (
            "Follow the examples with curl, jq and uuidgen. "
            "Use the API reference for endpoint parameters and responses."
            if english
            else "可使用 curl、jq 和 uuidgen 按顺序操作。各接口的参数和响应见 API 文档。"
        )
    )
    if reference_only:
        intro += (
            " Some v1 entry points on this server are retired. Use the v1 service URL in the examples."
            if english
            else " 当前服务已退役部分 v1 Agent 入口。请把示例地址换成你原先使用的 v1 服务。"
        )
    common_errors = (
        "For 401, check the Key and API version. For 409, check the current holding or booking. "
        "For 429 or a busy 503, follow Retry-After."
        if english
        else "遇到 401 时检查 Key 和 API 版本。遇到 409 时重新查占用或预约。429 或繁忙的 503 按 Retry-After 等待。"
    )
    steps = []
    for idx, (zh_title, en_title, zh_text, en_text, code) in enumerate(_V2_STEPS if version == 2 else _V1_STEPS):
        steps.append(
            f"<details{' open' if idx == 0 else ''}><summary>{escape(en_title if english else zh_title)}</summary>"
            f"<p>{escape(en_text if english else zh_text)}</p><pre><code>{escape(code)}</code></pre></details>"
        )
    return (
        f'<section class="agent-tutorial" id="quickstart"><h2>{title}</h2><p>{escape(intro)}</p>'
        + "".join(steps)
        + f"<p>{escape(common_errors)}</p></section>"
    )
