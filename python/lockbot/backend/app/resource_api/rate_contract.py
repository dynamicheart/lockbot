"""Publish the Agent API's route caps and shared request budgets in OpenAPI."""

import re

from lockbot.backend.app import config
from lockbot.backend.app.resource_api.ingress import (
    READ_LANE_LIMITS,
    READ_QUEUE_WAIT_SECONDS,
    READS_PER_KEY_IN_FLIGHT,
    V2_RESOURCE_LIST_PATH,
    V2_RESOURCE_LIST_SOURCE_LIMIT,
)
from lockbot.backend.app.resource_api.resource_changes import MAX_RESOURCE_STREAMS, MAX_RESOURCE_STREAMS_PER_IDENTITY

_V1 = {
    ("get", "/api/agent/context"): ("60/minute", "read"),
    ("get", "/api/agent/bindings"): ("60/minute", "read"),
    ("post", "/api/agent/bindings/recover"): ("60/minute", "read"),
    ("post", "/api/resource-auth/bindings"): ("10/hour", None),
    ("get", "/api/resource-auth/bindings/{binding_id}"): ("60/minute", None),
    ("post", "/api/resource-auth/credential-challenges"): ("10/hour", None),
    ("get", "/api/resource-auth/credential-challenges/{challenge_id}"): ("60/minute", None),
    ("get", "/resources"): ("120/minute", "read"),
    ("post", "/commands"): ("60/minute", "write"),
}
_V2 = {
    ("get", "/manifest"): (None, None),
    ("get", "/skill/lockbot.zip"): (None, None),
    ("post", "/bindings"): (config.RESOURCE_AUTH_V2_BIND_START_RATE, None),
    ("post", "/bindings/{binding_id}/complete"): ("60/minute", None),
    ("get", "/bindings"): ("120/minute", None),
    ("delete", "/bindings/{grant_ref}"): ("60/minute", None),
    ("get", "/resources"): (None, "read"),
    ("get", "/resources/events"): (None, "read"),
    ("post", "/commands"): ("60/minute", "write"),
    ("get", "/resources/{resource_ref}/metrics"): ("60/minute", "read"),
}
_V1_RESOURCE = re.compile(r"^/api/(?:agent|bots/\{bot_id\})(/.*)$")
_V2_PREFIX = "/api/agent/v2"


def describe_agent_rate(path, method, operation, *, lang="zh"):
    is_v2 = path.startswith(_V2_PREFIX + "/")
    route = path.removeprefix(_V2_PREFIX) if is_v2 else path
    if not is_v2 and (method, route) not in _V1:
        match = _V1_RESOURCE.fullmatch(path)
        if match:
            route = match[1]
    limits = (_V2 if is_v2 else _V1).get((method, route))
    if limits is None and not (method == "get" and path.startswith("/api/agent-skill")):
        return
    per_ip, budget = limits or ("60/minute", None)
    read = config.RESOURCE_API_READ_BUDGET_PER_MINUTE
    write = config.RESOURCE_API_WRITE_BUDGET_PER_MINUTE
    budget_limit = {"read": read, "write": write}.get(budget)
    interval = (
        {
            "active": config.RESOURCE_API_TIER_ACTIVE_SECONDS,
            "booked": config.RESOURCE_API_TIER_BOOKING_SECONDS,
            "discovery": config.RESOURCE_API_TIER_DISCOVERY_SECONDS,
        }
        if not is_v2 and route == "/resources" and method == "get"
        else None
    )
    info = {"route_per_ip": per_ip}
    resource_list = method == "get" and path == V2_RESOURCE_LIST_PATH
    watch = method == "get" and path == V2_RESOURCE_LIST_PATH + "/events"
    if resource_list:
        info["ingress_source_per_minute"] = V2_RESOURCE_LIST_SOURCE_LIMIT
        info["read_admission"] = {
            "lanes": READ_LANE_LIMITS,
            "queue_wait_seconds": READ_QUEUE_WAIT_SECONDS,
            "per_credential_in_flight": READS_PER_KEY_IN_FLIGHT,
        }
    if watch:
        info["concurrent_streams"] = {"per_identity": MAX_RESOURCE_STREAMS_PER_IDENTITY, "total": MAX_RESOURCE_STREAMS}
    if budget:
        info["shared_credential_budget"] = {"kind": budget, "per_minute": budget_limit}
    if interval:
        info["minimum_poll_seconds"] = interval
    pending_routes = {"/bindings", "/api/resource-auth/bindings", "/api/resource-auth/credential-challenges"}
    if method == "post" and route in pending_routes:
        info["outstanding_confirmation_retry_seconds"] = 600
    if is_v2 and (method, route) == ("post", "/bindings"):
        info["outstanding_confirmations_per_ip"] = config.RESOURCE_AUTH_V2_MAX_PENDING_PER_SOURCE
    operation["x-lockbot-rate-limit"] = info

    english = lang == "en"
    if resource_list:
        summary = (
            f"**Rate limit** — ingress protection at {V2_RESOURCE_LIST_SOURCE_LIMIT} requests/minute per source IP."
            if english
            else f"**限流**　入口保护为每来源 IP 每分钟 {V2_RESOURCE_LIST_SOURCE_LIMIT} 次。"
        )
        summary += (
            f" Each credential may have up to {READS_PER_KEY_IN_FLIGHT} active or queued queries in total."
            if english
            else f" 每个凭证正在处理和排队的资源查询，合计最多 {READS_PER_KEY_IN_FLIGHT} 个。"
        )
    elif per_ip:
        summary = f"**Rate limit** — {per_ip} per source IP." if english else f"**限流**　每来源 IP {per_ip}。"
    else:
        summary = "**Rate limit** — no route-specific limit." if english else "**限流**　无接口专属限流。"
    if "concurrent_streams" in info:
        summary += (
            f" Up to {MAX_RESOURCE_STREAMS_PER_IDENTITY} streams per identity and {MAX_RESOURCE_STREAMS} total."
            if english
            else f" 每个身份最多 {MAX_RESOURCE_STREAMS_PER_IDENTITY} 条并发连接，服务总计 {MAX_RESOURCE_STREAMS} 条。"
        )
    if budget:
        if english:
            summary += f" Uses the shared {budget} budget of {budget_limit} requests/minute per credential."
        else:
            kind = "读取" if budget == "read" else "写入"
            summary += f" 此操作计入同一凭证每分钟 {budget_limit} 次的共享{kind}额度。"
    if interval:
        values = "/".join(str(value) for value in interval.values())
        summary += (
            f" Minimum interval for active / waiting / discovery polls: {values}s."
            if english
            else f" 活跃／预约等待／发现资源的最短轮询间隔分别为 {values} 秒。"
        )
    if "outstanding_confirmations_per_ip" in info:
        capacity = info["outstanding_confirmations_per_ip"]
        summary += (
            f" Up to {capacity} codes may await confirmation per source IP."
            if english
            else f" 每来源 IP 最多有 {capacity} 个绑定码同时等待群确认。"
        )
    if "outstanding_confirmation_retry_seconds" in info:
        summary += (
            " Pending-confirmation capacity can also return 429 with Retry-After: 600."
            if english
            else " 有效的待确认请求达到上限时也会返回 429，Retry-After 为 600 秒。"
        )
    operation["description"] = "\n\n".join(filter(None, (operation.get("description"), summary)))
    if per_ip or budget:
        description = (
            "Rate limited. Honor Retry-After when present. Otherwise use exponential backoff."
            if english
            else "请求过频。有 Retry-After 时按其等待，否则使用指数退避。"
        )
        operation.setdefault("responses", {}).setdefault(
            "429",
            {
                "description": description,
                "headers": {"Retry-After": {"description": description, "schema": {"type": "integer", "minimum": 1}}},
            },
        )
    if resource_list or watch:
        description = (
            "Read capacity is busy. Wait for Retry-After before reconnecting or querying again."
            if english
            else "读取容量已满。按 Retry-After 指定的秒数等待后，再发起查询或订阅。"
        )
        operation.setdefault("responses", {}).setdefault(
            "503",
            {
                "description": description,
                "headers": {"Retry-After": {"schema": {"type": "integer", "minimum": 1}}},
            },
        )
