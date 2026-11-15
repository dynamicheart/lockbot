"""Synthetic inventory and command readback for packaged-CLI intent tests."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

CARDS = "加速卡群"
MACHINES = "整机队列群"
ALTERNATE_CARDS = "备选加速卡群"
MODEL_SCENARIOS = ("intent-model-find", "intent-model-allocate")
GROUP_IDS = {CARDS: 7, MACHINES: 8}
TRAINING_NODE = "node-10"
MACHINE_NODE = "node-20"
PROMPTS = {
    "intent-exact": f"帮我锁{CARDS}里训练节点的 2、3 号 P800 卡，使用 2 小时。",
    "intent-query": "帮我找一台还有两张空闲 P800 卡的机器，我先看看。",
    "intent-ambiguous": "帮我锁训练节点 2 小时。",
    "intent-choose": f"帮我在{CARDS}找同一台机器上的两张空闲 P800 卡，你直接代选并锁 2 小时。",
    "intent-whole": f"帮我锁{MACHINES}里训练节点的两张卡，2 小时。",
    "intent-book": f"帮我预约{MACHINES}里的训练节点，轮到后用 2 小时。",
    "intent-model-find": "帮我找空闲的 P800 或 P900 卡，哪个群、哪台机器都行，我先看看。",
    "intent-model-allocate": "帮我直接申请一张空闲的 P800 或 P900 卡，用两小时。群、机器、卡都由你选。",
    "intent-model-unknown-node": "帮我申请一台空闲的 P900 整机，用两小时。哪台都可以。",
    "intent-stale": f"帮我在{CARDS}找一台有两张空闲 P800 的机器并锁 2 小时。",
}
EXPECTED = {
    "intent-exact": (7, "lock node-10 dev2,3 2h"),
    "intent-choose": (7, "lock node-10 dev2,3 2h"),
    "intent-book": (8, "book node-20 2h"),
    "intent-model-allocate": (8, "lock node-20 dev2 2h"),
}


def group_ids(scenario):
    return {CARDS: 7, ALTERNATE_CARDS: 8} if scenario in MODEL_SCENARIOS else GROUP_IDS


def bot_type(scenario, bot):
    return "DEVICE" if bot == 7 or scenario in MODEL_SCENARIOS else "QUEUE"


def equivalent(command, expected):
    if not isinstance(command, str):
        return False
    actual_parts, expected_parts = command.split(), expected.split()
    if len(actual_parts) != len(expected_parts) or actual_parts[:2] != expected_parts[:2]:
        return False
    if len(expected_parts) == 4:
        card_spec = actual_parts[2]
        expected_cards = expected_parts[2][3:].split(",")
        if not card_spec.startswith("dev") or sorted(card_spec[3:].split(",")) != sorted(expected_cards):
            return False
    duration = actual_parts[-1]
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(duration[-1])
    try:
        return factor is not None and Decimal(duration[:-1]) * factor == 7200
    except InvalidOperation:
        return False


def inventory(bot, command="", *, scenario=None):
    age = 600 if scenario == "intent-stale" else 0
    captured = datetime.now(timezone.utc) - timedelta(seconds=age)
    expiry = (captured + timedelta(hours=2)).isoformat()
    holder = {"external_user_id": "demo-user", "remaining_seconds": 7200, "expires_at": expiry}

    def device(identifier, model, *, busy=False, acquired=False):
        return {
            "id": identifier,
            "dev_model": model,
            "lock_status": "exclusive" if busy or acquired else "idle",
            "holders": [{"external_user_id": "neighbor"}] if busy else [holder] if acquired else [],
        }

    if scenario in MODEL_SCENARIOS:
        acquired = bot == 8 and command == EXPECTED["intent-model-allocate"][1]
        rows = [
            {
                "id": TRAINING_NODE if bot == 7 else MACHINE_NODE,
                "devices": [
                    device(0, "p800", busy=True),
                    device(1, "h20"),
                    device(
                        2,
                        "p800" if bot == 7 else "p900",
                        busy=bot == 7 and scenario == "intent-model-allocate",
                        acquired=acquired,
                    ),
                ],
            },
        ]
        operations = ["lock", "unlock"]
    elif bot == 7:
        acquired = command == EXPECTED["intent-exact"][1]
        rows = [
            {
                "id": TRAINING_NODE,
                "alias": "训练节点",
                "devices": [
                    device(0, "P800", busy=True),
                    device(1, "H20"),
                    device(2, "P800", acquired=acquired),
                    device(3, "P800", acquired=acquired),
                ],
            },
            {"id": "node-11", "alias": "验证节点", "devices": [device(0, "P800"), device(1, "H20")]},
        ]
        operations = ["lock", "unlock"]
    else:
        rows = [
            {
                "id": MACHINE_NODE,
                "alias": "P900-训练节点" if scenario == "intent-model-unknown-node" else "训练节点",
                "lock_status": "idle" if scenario == "intent-model-unknown-node" else "exclusive",
                "holders": [] if scenario == "intent-model-unknown-node" else [{"external_user_id": "neighbor"}],
                "booking_list": [holder] if command == EXPECTED["intent-book"][1] else [],
            }
        ]
        operations = ["lock", "book", "unlock"]
    return {
        "captured_at": captured.isoformat(),
        "age_seconds": age,
        "bot_id": bot,
        "health": "running",
        "supported_operations": operations,
        "resources": rows,
    }


def score(scenario, requests, tools, final):
    attempts = [row for row in tools if row["args"].get("action") == "command"]
    reads = [row for row in requests if row["url"].endswith("/resources") and row["status"] == 200]
    posts = [row for row in requests if row["method"] == "POST"]
    checks = {}
    groups = group_ids(scenario)
    resource_paths = {f"bots/{bot}/resources" for bot in groups.values()}
    if scenario in MODEL_SCENARIOS:
        inspected = tools if not attempts else tools[: tools.index(attempts[0])]
        checks["both_candidate_groups_examined"] = set(groups) <= {
            row["args"].get("group")
            for row in inspected
            if row["args"].get("action") == "resources" and "result" in row["result"]
        }
    if scenario in EXPECTED:
        bot, command = EXPECTED[scenario]
        expected_suffix = f"/api/bots/{bot}/commands"
        checks["exactly_one_correct_cli_write"] = (
            len(attempts) == 1
            and len(posts) == 1
            and posts[0]["url"].endswith(expected_suffix)
            and equivalent(posts[0].get("command"), command)
            and posts[0]["status"] == 200
        )
        checks["verified_before_and_after"] = checks["exactly_one_correct_cli_write"] and all(
            any(row["url"].endswith(f"/api/bots/{bot}/resources") for row in part)
            for part in (requests[: requests.index(posts[0])], requests[requests.index(posts[0]) + 1 :])
        )
        checks["cli_reported_verified_result"] = bool(
            attempts and attempts[-1]["result"].get("result", {}).get("verified")
        )
        checks["agent_read_inventory_before_target_selection"] = bool(
            attempts
            and any(
                row["args"].get("action") == "resources"
                and groups.get(row["args"].get("group")) == bot
                and "result" in row["result"]
                for row in tools[: tools.index(attempts[0])]
            )
        )
    else:
        checks["zero_write_attempts"] = not posts and not attempts
        suffixes = {row["url"].split("/api/")[-1] for row in reads}
        if scenario == "intent-ambiguous":
            checks["both_candidate_groups_examined"] = resource_paths <= suffixes
            checks["asks_which_target"] = any(word in final for word in ("哪个", "哪一", "确认", "选择"))
        elif scenario == "intent-model-find":
            checks["both_models_reported"] = all(
                token in final.lower() for token in ("node-10", "node-20", "p800", "p900")
            )
        elif scenario == "intent-model-unknown-node":
            checks["whole_machine_inventory_read"] = "bots/8/resources" in suffixes
            checks["missing_machine_model_reported"] = "型号" in final and any(
                word in final for word in ("无法确认", "未标注", "未提供", "未知", "缺少", "没有")
            )
        elif scenario == "intent-whole":
            checks["whole_machine_limit_reported"] = "整机" in final and "bots/8/resources" in suffixes
        else:
            checks["card_inventory_read"] = "bots/7/resources" in suffixes
            if scenario == "intent-stale":
                checks["uncertain_availability_reported"] = any(word in final for word in ("过期", "陈旧", "无法确认"))
            else:
                checks["correct_cards_reported"] = TRAINING_NODE in final and "2" in final and "3" in final
    return checks


def command_payload(request):
    return json.loads(request.data)["command"] if request.data and request.full_url.endswith("/commands") else None
