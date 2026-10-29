"""Render current Infoflow replies as Feishu posts, without connecting any resource bot.

Dry-run by default. Real delivery requires an explicit private target file and --send.
This diagnostic is not a production Feishu adapter or webhook implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
import uuid
from pathlib import Path
from string import Formatter

import requests
from lockbot.core.device_bot import DeviceBot
from lockbot.core.i18n import t
from lockbot.core.node_bot import NodeBot
from lockbot.core.platforms.infoflow import InfoflowAdapter
from lockbot.core.queue_bot import QueueBot
from lockbot.core.queue_device_bot import QueueDeviceBot


class CaptureAdapter(InfoflowAdapter):
    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return [(200, "diagnostic-capture-only")]


def render_post(message, *, identities=None, title=""):
    """Preserve native TEXT/LINK/AT boundaries; never parse user prose as markup."""
    identities = identities or {}
    rows = [[]]
    for item in message["message"]["body"]:
        kind = item["type"]
        if kind == "TEXT":
            for index, line in enumerate(item.get("content", "").split("\n")):
                if index:
                    rows.append([])
                if line:
                    rows[-1].append({"tag": "text", "text": line})
        elif kind == "LINK":
            href = item.get("href", "")
            if not href.startswith(("http://", "https://")):
                raise ValueError("Only HTTP(S) links are supported")
            rows[-1].append({"tag": "a", "text": item.get("label") or href, "href": href})
        elif kind == "AT":
            if item.get("atall"):
                rows[-1].append({"tag": "at", "user_id": "all"})
            for person in item.get("atuserids", []):
                if person in identities:
                    rows[-1].append({"tag": "at", "user_id": identities[person]})
                else:
                    # Fixture people are labels, never guessed real mention identities.
                    rows[-1].append({"tag": "text", "text": f"@{person}（虚构用户）"})
        else:
            raise ValueError(f"Unsupported reply fragment: {kind}")
    # Feishu post rows must contain nodes. A text space preserves deliberate blank lines.
    rows = [row or [{"tag": "text", "text": " "}] for row in rows]
    return {"zh_cn": {"title": title, "content": rows}}


def gallery():
    """Use live core command/help renderers on disposable local state only."""
    output = []
    adapter = CaptureAdapter()

    def add(name, message):
        output.append({"name": name, "infoflow": message})

    def text(name, content, users=()):
        add(name, adapter.build_reply(content, list(users), group_id="1"))

    text(
        "intro",
        (
            "🧪【飞书消息渲染联调】\n\n以下均为虚构资源样例，不代表真实资源申请。\n"
            "核对 emoji、链接、@、中文／英文、空行和长消息。"
        ),
    )
    with tempfile.TemporaryDirectory(prefix="lockbot-gallery-") as directory:
        for kind, bot_class in (
            ("NODE", NodeBot),
            ("DEVICE", DeviceBot),
            ("QUEUE", QueueBot),
            ("QUEUE_DEVICE", QueueDeviceBot),
        ):
            cards = kind in {"DEVICE", "QUEUE_DEVICE"}
            first_node = "demo_node_1" if kind == "DEVICE" else "demo-node-1"
            second_node = "demo_node_2" if kind == "DEVICE" else "demo-node-2"
            config = {
                "BOT_TYPE": kind,
                "BOT_ID": kind,
                "DATA_DIR": str(Path(directory) / kind),
                "CLUSTER_CONFIGS": {first_node: ["h20", "h20"], second_node: ["a100", "a100"]}
                if cards
                else [first_node, second_node],
                "PLATFORM_URL": "https://lockbot.example.test",
                "RESOURCE_AUTH_ENABLED": True,
                "EARLY_NOTIFY": False,
            }
            bot = bot_class(config_dict=config, adapter=adapter)
            add(f"{kind.lower()}-help", bot.print_help("alice"))
            target = f"{first_node} dev0" if cards else first_node
            add(f"{kind.lower()}-lock", bot.lock("alice", f"lock {target} 20m"))
            if "QUEUE" in kind:
                add(f"{kind.lower()}-book", bot.book("bob", f"book {target} 5m"))
            add(f"{kind.lower()}-query", bot.query("alice"))
            add(f"{kind.lower()}-release", bot.unlock("alice", f"free {target}"))
    text(
        "links",
        [
            "🔗【超链接与特殊字符】\n\n",
            ("Agent 使用：", "https://lockbot.example.test/api/agent-skill/SKILL.md"),
            ("GitHub：", "https://github.com/dynamicheart/lockbot"),
            "\n字面文本：<at user_id=all> 不应产生提及；A&B；**普通文本**。\n",
        ],
    )
    for key, suffix in [
        ("alert.early_time_remaining", "demo-node-1 alice 5 分钟\n\n"),
        ("alert.auto_released_title", "demo-node-1 alice\n\n"),
        ("alert.your_turn", "请在 5 分钟内 lock demo-node-1\n\n"),
        ("alert.booking_expired", "demo-node-1 bob\n\n"),
        ("notify.resource_available_header", "🗓️ 目前待抢锁的预约：\n  - demo-node-1 bob 5 分钟\n\n"),
        ("opkick.success", "demo-node-1 alice\n\n"),
    ]:
        text(
            key,
            t(key, time_alert="5 分钟", timeout="5 分钟", operator_id="demo-op", target_user="alice") + suffix,
            ["alice"],
        )
    binding_keys = [
        key for key in __import__("lockbot.core.i18n.zh", fromlist=["MESSAGES"]).MESSAGES if key.startswith("binding.")
    ]
    for key in binding_keys:
        if key == "binding.agent_usage_hint":
            continue
        text(
            key,
            t(key, bot_id=1, binding_id="fixture-reference", label="公开测试用途", last_used="刚刚") + "\n\n",
            ["alice"],
        )
    # Exercise the rest of the user-facing error/alias templates with explicit fixtures.
    from lockbot.core.i18n.zh import MESSAGES

    values = dict(
        user_id="alice",
        node_key="demo-node-1",
        valid_keys="demo-node-1, demo-node-2",
        command="demo",
        max_duration="2 小时",
        whitelist_hint="",
        owner="demo-admin",
        dev_min=0,
        dev_max=1,
        num_devs=2,
        limit=2,
        alias="测试节点",
        node="demo-node-1",
        num=9,
        operator_id="demo-op",
        target_user="alice",
        dev_id=0,
        reason="联调样例",
    )
    for prefix in ("error.", "alias.", "opkick."):
        samples = []
        for key, template in MESSAGES.items():
            if not key.startswith(prefix):
                continue
            fields = {name for _, name, _, _ in Formatter().parse(template) if name}
            sample = template.format(**{name: values.get(name, "示例") for name in fields})
            samples.append(f"{key}\n{sample.strip()}")
        for start in range(0, len(samples), 6):
            text(f"{prefix[:-1]}-templates-{start // 6 + 1}", "\n\n".join(samples[start : start + 6]) + "\n")
    text(
        "agent-operation",
        ("🤖【Agent API操作】\n\n用户：alice\n命令：lock demo-node-1 20m\n任务分类：模型性能测试\n\n"),
    )
    text("english", t("success.resource_locked", lang="en") + "demo-node-1 alice 20 minutes\n\n")
    text("emoji", "✅ ❌ ℹ️ ⚠️ ❗️ 📖 🤖 🗓️ ⌛️ 📢 🔧 🏁\n\n普通空行之后仍保持正文。\n")
    text("mention-all", "📢【@全体测试】\n\n仅验证飞书群通知渲染，不修改资源。\n\n")
    output[-1]["infoflow"]["message"]["body"].append({"type": "AT", "atall": True})
    return output


_session = requests.Session()


def api(token, path, data):
    response = _session.post(
        "https://open.feishu.cn/open-apis/" + path,
        json=data,
        headers={"Authorization": "Bearer " + token},
        timeout=(8, 15),
        allow_redirects=False,
    )
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError("Feishu rejected request, code=invalid_response")
    if not 200 <= response.status_code < 300 or body.get("code") != 0:
        code = body.get("code")
        code = code if type(code) is int else "invalid_response"
        raise RuntimeError(f"Feishu rejected request, code={code}")
    return body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-file", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Reuse report UUIDs and skip accepted messages")
    parser.add_argument("--only", help="Send one named case instead of the complete suite")
    args = parser.parse_args()
    cases = gallery()
    target = json.loads(args.target_file.read_text()) if args.target_file else {}
    if args.send and not all(target.get(key) for key in ("chat_id", "tenant_access_token")):
        parser.error("--send requires an explicitly selected chat and an existing application token")
    if target.get("mention_id"):
        message = CaptureAdapter().build_reply(
            "📢【@用户测试】\n\n资源可用，请在 5 分钟内领取（虚构提醒）。\n\n", ["self"], group_id="1"
        )
        cases.append({"name": "mention-user", "infoflow": message})
    if args.only:
        cases = [case for case in cases if case["name"] == args.only]
        if not cases:
            parser.error("Unknown or unavailable case")
    report = {
        "run_id": str(uuid.uuid4()),
        "sent": args.send,
        "cases": [],
        "single_user_mention_configured": bool(target.get("mention_id")),
        "target_scope": hashlib.sha256(
            json.dumps([target.get("chat_id"), target.get("mention_id")]).encode()
        ).hexdigest(),
    }
    expected_scope = report["target_scope"]
    if args.resume:
        report = json.loads(args.report.read_text())
        if not report.get("sent") or not args.send:
            parser.error("Only a delivery report can be resumed")
        if report.get("target_scope") != expected_scope:
            parser.error("Resume requires the original selected chat and mention target")
    report["single_user_mention_configured"] = bool(target.get("mention_id"))
    completed = {record["name"]: record for record in report["cases"] if record["status"] == "accepted"}
    for index, case in enumerate(cases, 1):
        if case["name"] in completed:
            continue
        report["cases"] = [record for record in report["cases"] if record["name"] != case["name"]]
        post = render_post(
            case["infoflow"],
            identities={"self": target["mention_id"]} if target.get("mention_id") else {},
            title=f"{index:02} · {case['name']}",
        )
        record = {"name": case["name"], "post": post, "status": "dry_run"}
        if args.send:
            try:
                reply = api(
                    target["tenant_access_token"],
                    "im/v1/messages?receive_id_type=chat_id",
                    {
                        "receive_id": target["chat_id"],
                        "msg_type": "post",
                        "content": json.dumps(post, ensure_ascii=False),
                        "uuid": str(uuid.uuid5(uuid.UUID(report["run_id"]), case["name"])),
                    },
                )
                message_id = reply.get("data", {}).get("message_id")
                if not isinstance(message_id, str) or not message_id:
                    raise ValueError("Invalid delivery acknowledgement")
                record.update(status="accepted", message_id=message_id)
            except (OSError, requests.RequestException, ValueError, RuntimeError, AttributeError) as exc:
                # Do not retry an ambiguous send or leak tokens/full API bodies into output.
                record.update(
                    status="failed_or_uncertain",
                    error=type(exc).__name__,
                    detail=str(exc) if isinstance(exc, RuntimeError) else "transport_or_format_failure",
                )
                report["cases"].append(record)
                args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                raise SystemExit("Delivery stopped; inspect the report, no automatic retries") from None
            time.sleep(0.6)
        report["cases"].append(record)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "cases": len(cases),
                "sent": args.send,
                "single_user_mention_configured": report["single_user_mention_configured"],
            }
        )
    )


if __name__ == "__main__":
    main()
