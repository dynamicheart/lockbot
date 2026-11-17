"""Synthetic inventory and command readback for packaged-CLI intent tests."""

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

CARDS = "加速卡群"
MACHINES = "整机队列群"
ALTERNATE_CARDS = "备选加速卡群"
MODEL_SCENARIOS = ("intent-model-find", "intent-model-allocate")
WHOLE_MACHINE_MODELS = {
    "intent-model-whole-machine": "P900",
    "intent-model-p800-whole-machine": "P800",
}
UNVERIFIED_MACHINE_MODELS = {
    "intent-model-unknown-node": "P900",
    "intent-model-p800-unknown-node": "P800",
}
GROUP_IDS = {CARDS: 7, MACHINES: 8}
TRAINING_NODE = "node-10"
MACHINE_NODE = "node-20"
MATCHING_MACHINE = "node-23"
WHOLE_MACHINE_BOOKING = f"book {MATCHING_MACHINE} 2h"
SHORT_PROMPTS = {
    "intent-lock-tail": "lock 19",
    "intent-lock-tail-list": "lock 19,20,21,22",
    "intent-lock-tail-list-4h": "lock 11,12,13,14 4h",
    "intent-lock-duration-only": "lock 一天",
    "intent-find-p800": "帮我找台P800的机器",
    "intent-book-tail": "book 19 4h",
    "intent-shared-tail": "slock 19 dev0 4h",
    "intent-ambiguous-tail": "lock 19 4h",
}
CONVERSATIONAL_PROMPTS = {
    "intent-context-renew": ("lock 19 2h", "再续两小时"),
    "intent-context-release": ("lock 19 2h", "用完了，释放吧"),
    "intent-context-lock-day": ("帮我找台P800的机器", "lock 一天"),
}
OWN_RESOURCE_PROMPTS = {
    "intent-my-locks": "我现在lock了哪些机器",
    "intent-my-books": "我现在book了哪些机器",
    "intent-reminder-configured": "准备到时间时提醒吧",
    "intent-reminder-unavailable": "准备到时间时提醒吧",
}
TASK_SCENARIOS = {*CONVERSATIONAL_PROMPTS, *OWN_RESOURCE_PROMPTS}

SHORT_MACHINE_IDS = {
    "intent-lock-tail": (19,),
    "intent-lock-tail-list": (19, 20, 21, 22),
    "intent-lock-tail-list-4h": (11, 12, 13, 14),
    "intent-book-tail": (19,),
}
SHORT_DEFAULT_DURATION = 10_800
SHORT_DEFAULT_SCENARIOS = {"intent-lock-tail", "intent-lock-tail-list"}

PROMPTS = {
    **SHORT_PROMPTS,
    **OWN_RESOURCE_PROMPTS,
    **{scenario: turns[0] for scenario, turns in CONVERSATIONAL_PROMPTS.items()},
    "intent-exact": "锁 node-10 的 2、3 号 P800 卡，两小时。",
    "intent-query": "哪台机器还有两张空闲 P800？我先看看。",
    "intent-ambiguous": "锁训练节点 2 小时。",
    "intent-choose": "找台机器，直接锁两张空闲 P800，用两小时。",
    "intent-whole": "锁 node-20 上两张卡，两小时。",
    "intent-book": "book node-20 2h",
    "intent-claim": "lock 20",
    "intent-model-find": "哪个群有空闲的 P800 或 P900 卡？我先看看。",
    "intent-model-allocate": "直接锁一张空闲 P800 或 P900 卡，两小时。你挑。",
    **{
        scenario: f"申请台空闲 {model} 整机，两小时。哪台都行。"
        for scenario, model in UNVERIFIED_MACHINE_MODELS.items()
    },
    **{
        scenario: f"直接锁台空闲整机，至少有两张 {model} 卡，用两小时。你挑。"
        for scenario, model in WHOLE_MACHINE_MODELS.items()
    },
    "intent-renew": "我在用的机器续两小时。",
    "intent-release": "释放 node-10 的 2 号卡，3 号留着。",
    "intent-write-unknown": "锁 node-10 的 2、3 号卡，两小时。",
    "intent-stale": "找同一台机器的两张空闲 P800，锁两小时。",
}
EXPECTED = {
    "intent-lock-tail": (8, "lock node-19 3h"),
    "intent-lock-tail-list": (8, "lock node-19,node-20,node-21,node-22 3h"),
    "intent-lock-tail-list-4h": (8, "lock node-11,node-12,node-13,node-14 4h"),
    "intent-book-tail": (8, "book node-19 4h"),
    "intent-shared-tail": (7, "slock node-19 dev0 4h"),
    "intent-exact": (7, "lock node-10 dev2,3 2h"),
    "intent-choose": (7, "lock node-10 dev2,3 2h"),
    "intent-book": (8, "book node-20 2h"),
    "intent-claim": (8, "lock node-20"),
    "intent-renew": (8, "lock node-20 2h"),
    "intent-release": (7, "unlock node-10 dev2"),
    "intent-write-unknown": (7, "lock node-10 dev2,3 2h"),
    "intent-model-allocate": (8, "lock node-20 dev2 2h"),
    **{scenario: (8, f"lock {MATCHING_MACHINE} 2h") for scenario in WHOLE_MACHINE_MODELS},
}


def group_ids(scenario):
    return {CARDS: 7, ALTERNATE_CARDS: 8} if scenario in MODEL_SCENARIOS else GROUP_IDS


def bot_type(scenario, bot):
    if scenario == "intent-my-books" and bot == 7:
        return "QUEUE_DEVICE"
    return "DEVICE" if bot == 7 or scenario in MODEL_SCENARIOS else "QUEUE"


def equivalent(command, expected, *, default_duration=None):
    if not isinstance(command, str):
        return False
    actual_parts, expected_parts = command.split(), expected.split()
    if default_duration is not None and len(actual_parts) == 2 and len(expected_parts) == 3:
        actual_parts.append(f"{default_duration}s")
    if len(actual_parts) != len(expected_parts) or actual_parts[0] != expected_parts[0]:
        return False
    if sorted(actual_parts[1].split(",")) != sorted(expected_parts[1].split(",")):
        return False
    if len(expected_parts) == 2:
        return True
    if expected_parts[-1].startswith("dev"):
        return actual_parts[-1] == expected_parts[-1]
    if len(expected_parts) == 4:
        card_spec = actual_parts[2]
        expected_cards = expected_parts[2][3:].split(",")
        if not card_spec.startswith("dev") or sorted(card_spec[3:].split(",")) != sorted(expected_cards):
            return False
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    actual, expected = actual_parts[-1], expected_parts[-1]
    try:
        return Decimal(actual[:-1]) * units[actual[-1]] == Decimal(expected[:-1]) * units[expected[-1]]
    except (InvalidOperation, KeyError):
        return False


def inventory(bot, command="", *, scenario=None, phase=1, writes=0):
    if scenario in TASK_SCENARIOS:
        return task_inventory(bot, scenario, phase, writes)
    age = 600 if scenario == "intent-stale" else 0
    captured = datetime.now(timezone.utc) - timedelta(seconds=age)
    expiry = (captured + timedelta(hours=2)).isoformat()
    holder = {"external_user_id": "demo-user", "remaining_seconds": 7200, "expires_at": expiry}
    released = scenario == "intent-release" and command == EXPECTED["intent-release"][1]

    def device(identifier, model, *, busy=False, acquired=False):
        return {
            "id": identifier,
            "dev_model": model,
            "lock_status": "exclusive" if busy or acquired else "idle",
            "holders": [{"external_user_id": "neighbor"}] if busy else [holder] if acquired else [],
        }

    if scenario in SHORT_PROMPTS:
        acquired = bool(command and scenario in EXPECTED and command == EXPECTED[scenario][1])
        if acquired:
            value = command.split()[-1]
            seconds = int(value[:-1]) * 3600
            holder.update(remaining_seconds=seconds, expires_at=(captured + timedelta(seconds=seconds)).isoformat())
        if bot == 7:
            matching = scenario in {"intent-ambiguous-tail", "intent-shared-tail"}
            rows = [
                {
                    "id": "node-19" if matching else "node-10",
                    "alias": "19" if matching else "10",
                    "devices": [device(0, "P800", acquired=acquired)],
                }
            ]
            if acquired:
                rows[0]["devices"][0]["lock_status"] = "shared"
            operations = ["lock", "slock", "unlock"]
        else:
            numbers = SHORT_MACHINE_IDS.get(scenario, (23,) if scenario == "intent-find-p800" else (19, 20))
            if scenario == "intent-shared-tail":
                numbers = (20,)
            booking = scenario == "intent-book-tail"
            holders = [{"external_user_id": "neighbor"}] if booking else [holder] if acquired else []
            rows = [
                {
                    "id": f"node-{number}",
                    "alias": str(number),
                    "lock_status": "exclusive" if holders else "idle",
                    "holders": holders,
                    "booking_list": [holder] if acquired and booking else [],
                    "accelerators": [{"model": "P800", "count": 8}],
                }
                for number in numbers
            ]
            operations = ["lock", "book", "unlock"]
    elif scenario in MODEL_SCENARIOS:
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
                    device(2, "P800", acquired=acquired or (scenario == "intent-release" and not released)),
                    device(3, "P800", acquired=acquired or scenario == "intent-release"),
                ],
            },
            {"id": "node-11", "alias": "验证节点", "devices": [device(0, "P800"), device(1, "H20")]},
        ]
        operations = ["lock", "unlock"]
    elif scenario in WHOLE_MACHINE_MODELS:
        model = WHOLE_MACHINE_MODELS[scenario]
        other_model = "P800" if model == "P900" else "P900"
        acquired = command == EXPECTED[scenario][1]
        rows = [
            {"id": MACHINE_NODE, "alias": f"{model}-训练节点", "lock_status": "idle", "holders": []},
            {
                "id": "node-21",
                "lock_status": "idle",
                "holders": [],
                "accelerators": [{"model": model, "count": 1}, {"model": other_model, "count": 7}],
            },
            {
                "id": "node-22",
                "lock_status": "exclusive",
                "holders": [{"external_user_id": "neighbor"}],
                "accelerators": [{"model": model, "count": 8}],
            },
            {
                "id": MATCHING_MACHINE,
                "lock_status": "exclusive" if acquired else "idle",
                "holders": [holder] if acquired else [],
                "booking_list": [holder] if command == WHOLE_MACHINE_BOOKING else [],
                "claimable_by_you": command == WHOLE_MACHINE_BOOKING,
                "accelerators": [{"model": "P800", "count": 6}, {"model": "P900", "count": 2}],
            },
        ]
        operations = ["lock", "book", "unlock"]
    elif scenario == "intent-renew":
        remaining_minutes = 150 if command == EXPECTED[scenario][1] else 30
        holder.update(
            remaining_seconds=remaining_minutes * 60,
            expires_at=(captured + timedelta(minutes=remaining_minutes)).isoformat(),
        )
        rows = [{"id": MACHINE_NODE, "alias": "训练节点", "lock_status": "exclusive", "holders": [holder]}]
        operations = ["lock", "book", "unlock"]
    elif scenario == "intent-claim":
        acquired = command == EXPECTED[scenario][1]
        rows = [
            {
                "id": MACHINE_NODE,
                "alias": "训练节点",
                "lock_status": "exclusive" if acquired else "idle",
                "holders": [holder] if acquired else [],
                "booking_list": [] if acquired else [holder],
                "claimable_by_you": not acquired,
                "claim_command": None if acquired else f"lock {MACHINE_NODE}",
            },
        ]
        operations = ["lock", "book", "unlock"]
    else:
        model = UNVERIFIED_MACHINE_MODELS.get(scenario)
        rows = [
            {
                "id": MACHINE_NODE,
                "alias": f"{model}-训练节点" if model else "训练节点",
                "lock_status": "idle" if model else "exclusive",
                "holders": [] if model else [{"external_user_id": "neighbor"}],
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


def score(scenario, requests, tools, final, *, phase=1):
    if scenario in TASK_SCENARIOS:
        return task_score(scenario, phase, requests, tools, final)
    attempts = [row for row in tools if row["args"].get("action") == "command"]
    reads = [row for row in requests if row["url"].endswith("/resources") and row["status"] == 200]
    posts = [row for row in requests if row["method"] == "POST"]
    checks = {}
    groups = group_ids(scenario)
    resource_paths = {f"bots/{bot}/resources" for bot in groups.values()}
    inspected = tools if not attempts else tools[: tools.index(attempts[0])]
    if scenario in MODEL_SCENARIOS or scenario in SHORT_PROMPTS.keys() - {"intent-lock-duration-only"}:
        checks["both_candidate_groups_examined"] = set(groups) <= {
            row["args"].get("group")
            for row in inspected
            if row["args"].get("action") == "resources" and "result" in row["result"]
        }
    if scenario in EXPECTED:
        bot, command = EXPECTED[scenario]
        default_duration = SHORT_DEFAULT_DURATION if scenario in SHORT_DEFAULT_SCENARIOS else None
        expected_suffix = f"/api/bots/{bot}/commands"
        unknown = scenario == "intent-write-unknown"
        operation = {"intent-release": "release", "intent-renew": "renew", "intent-claim": "claim"}.get(scenario)
        checks["exactly_one_correct_cli_write"] = (
            len(attempts) == 1
            and len(posts) == 1
            and posts[0]["url"].endswith(expected_suffix)
            and equivalent(posts[0].get("command"), command, default_duration=default_duration)
            and posts[0]["status"] == (504 if unknown else 200)
            and (operation is None or attempts[0]["args"].get("operation") == operation)
        )
        checks["verified_before_and_after"] = checks["exactly_one_correct_cli_write"] and mutation_observed(
            requests, posts[0], attempts[0], bot
        )
        if scenario in SHORT_DEFAULT_SCENARIOS:
            used_cli_default = bool(
                attempts
                and "duration" not in attempts[0]["args"]
                and attempts[0]["result"].get("result", {}).get("duration_source") == "group-default"
            )
            checks["verified_default_duration"] = used_cli_default or any(
                row["args"].get("action") == "context"
                and groups.get(row["args"].get("group")) == bot
                and row["result"].get("result", {}).get("bot_rules", {}).get("default_duration_seconds")
                == SHORT_DEFAULT_DURATION
                for row in inspected
            )
        if unknown:
            checks["unknown_outcome_reported"] = any(
                phrase in final for phrase in ("无法确认", "未能确认", "未确认", "结果不明", "结果未知", "不确定")
            )
        else:
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
        if scenario in WHOLE_MACHINE_MODELS:
            model = WHOLE_MACHINE_MODELS[scenario].lower()
            checks["whole_machine_result_reported"] = (
                MATCHING_MACHINE in final and model in final.lower() and "整机" in final
            )
        if scenario in {"intent-claim", "intent-renew"}:
            checks["target_and_duration_reported"] = any(name in final for name in (MACHINE_NODE, "训练节点")) and any(
                value in final.replace(" ", "")
                for value in ("两小时", "2小时", "120分钟", "7200秒", "2h", "150分钟", "2小时30分", "2.5小时")
            )
        if scenario == "intent-release":
            checks["released_and_retained_cards_reported"] = (
                any(name in final for name in (TRAINING_NODE, "训练节点"))
                and {"2", "3"} <= set(final)
                and "释放" in final
            )
    else:
        checks["zero_write_attempts"] = not posts and not attempts
        suffixes = {row["url"].split("/api/")[-1] for row in reads}
        if scenario in {"intent-ambiguous", "intent-ambiguous-tail"}:
            checks["both_candidate_groups_examined"] = resource_paths <= suffixes
            explicit_question = re.search(
                r"哪(?:个|一个|一台|台)[^。！？!?]{0,8}(?:群|节点|机器|资源)[^。！？!?\n]{0,16}[？?]"
                r"|(?:^|[.!?\n])\s*which\s+(?:group|node|machine|resource)\s+"
                r"(?:should|shall|do|would)\s+(?:i|we|you)\b[^.!?\n]{0,100}\?",
                final,
                re.IGNORECASE,
            )
            both_targets = all(name in final for name in (CARDS, MACHINES)) or all(
                node in final for node in (TRAINING_NODE, MACHINE_NODE)
            )
            choice = re.search(
                r"(?:请|麻烦|需要[你您])[^\n。！？!?]{0,10}(?:选|确认)"
                r"|[你您](?:想|要|希望)[^。！？!?]+还是[^。！？!?]+[？?]"
                r"|(?:请)?告诉我[你您](?:指|要)(?:的是)?哪(?:一个|个|一台|台)"
                r"|[你您](?:想|要|希望)(?:怎么|如何)处理\s*[？?]"
                r"|[你您](?:想|要|希望)(?:选|锁|用)?哪(?:一个|个|一台|台)\s*[？?]",
                final,
            )
            checks["asks_which_target"] = bool(explicit_question or (both_targets and choice))
        elif scenario == "intent-lock-duration-only":
            checks["asks_for_missing_target"] = bool(
                re.search(r"(?:选|锁|哪|目标|指定).{0,10}(?:机器|节点|资源|台)", final)
            )
        elif scenario == "intent-find-p800":
            checks["whole_p800_candidate_reported"] = "node-23" in final and "p800" in final.lower()
        elif scenario == "intent-model-find":
            checks["both_models_reported"] = all(
                token in final.lower() for token in ("node-10", "node-20", "p800", "p900")
            )
        elif scenario in UNVERIFIED_MACHINE_MODELS:
            checks["whole_machine_inventory_read"] = "bots/8/resources" in suffixes
            checks["missing_machine_model_reported"] = "型号" in final and any(
                word in final for word in ("无法确认", "不能确认", "未登记", "未标注", "未提供", "未知", "缺少", "没有")
            )
        elif scenario == "intent-whole":
            checks["whole_machine_limit_reported"] = "整机" in final and "bots/8/resources" in suffixes
        else:
            checks["card_inventory_read"] = "bots/7/resources" in suffixes
            if scenario == "intent-stale":
                checks["uncertain_availability_reported"] = any(
                    word in final for word in ("过期", "陈旧", "无法确认")
                ) or bool(
                    re.search(
                        r"快照[^。！？\n]{0,12}滞后\s*\d+\s*(?:秒|分钟|小时)"
                        r"|快照 *(?:仍然|仍|还)? *不(?:够)?新鲜",
                        final,
                    )
                )
            else:
                checks["correct_cards_reported"] = TRAINING_NODE in final and "2" in final and "3" in final
    return checks


def command_payload(request):
    return json.loads(request.data)["command"] if request.data and request.full_url.endswith("/commands") else None


def task_expectation(scenario, phase):
    """A second turn keeps the observed target while omitting it from user text."""
    if scenario not in CONVERSATIONAL_PROMPTS:
        return None
    if phase == 1:
        return None if scenario == "intent-context-lock-day" else (8, "lock node-19 2h", "lock")
    return {
        "intent-context-renew": (8, "lock node-19 2h", "renew"),
        "intent-context-release": (8, "unlock node-19", "release"),
        "intent-context-lock-day": (8, "lock node-23 1d", "lock"),
    }[scenario]


def task_inventory(bot, scenario, phase, writes):
    now = datetime.now(timezone.utc)

    def person(name="demo-user", seconds=7200):
        return {
            "external_user_id": name,
            "remaining_seconds": seconds,
            "expires_at": (now + timedelta(seconds=seconds)).isoformat(),
        }

    def machine(identifier, *, owner=None, bookings=(), model="P800", seconds=7200):
        return {
            "id": identifier,
            "alias": identifier.removeprefix("node-"),
            "lock_status": "exclusive" if owner else "idle",
            "holders": [person(owner, seconds)] if owner else [],
            "booking_list": [person(name) for name in bookings],
            "accelerators": [{"model": model, "count": 8}],
        }

    def card(identifier, owner=None, bookings=()):
        return {
            "id": identifier,
            "dev_model": "P800",
            "lock_status": "exclusive" if owner else "idle",
            "holders": [person(owner)] if owner else [],
            "booking_list": [person(name) for name in bookings],
        }

    if scenario in {"intent-my-locks", "intent-my-books"}:
        if bot == 7:
            rows = [
                {
                    "id": TRAINING_NODE,
                    "devices": [
                        card(0, "neighbor"),
                        card(2, "demo-user"),
                        card(3, "neighbor", ("demo-user",)),
                        card(4, "demo-user-other"),
                    ],
                }
            ]
        else:
            rows = [
                machine("node-20", owner="demo-user"),
                machine("node-21", owner="neighbor", bookings=("demo-user",)),
                machine("node-22", owner="neighbor", bookings=("neighbor",)),
            ]
    elif scenario == "intent-context-lock-day":
        rows = (
            [{"id": TRAINING_NODE, "devices": [card(0, "neighbor")]}]
            if bot == 7
            else [
                machine("node-23", owner="demo-user" if phase == 2 and writes else None, seconds=86400),
                machine("node-24", model="P900"),
            ]
        )
    else:
        held = scenario in OWN_RESOURCE_PROMPTS or writes > 0
        released = scenario == "intent-context-release" and phase == 2 and writes >= 2
        seconds = 14400 if scenario == "intent-context-renew" and phase == 2 and writes >= 2 else 7200
        rows = (
            [{"id": TRAINING_NODE, "devices": [card(0, "neighbor")]}]
            if bot == 7
            else [
                machine("node-19", owner="demo-user" if held and not released else None, seconds=seconds),
                machine("node-20", owner="demo-user"),
                machine("node-21", owner="neighbor"),
            ]
        )
    snapshot = {
        "captured_at": now.isoformat(),
        "age_seconds": 0,
        "bot_id": bot,
        "bot_type": bot_type(scenario, bot),
        "health": "running",
        "actor": {"platform": "infoflow", "external_user_id": "demo-user"},
        "supported_operations": ["lock", "book", "unlock"],
        "resources": rows,
    }
    if scenario.startswith("intent-reminder-") and bot == 8:
        configured = scenario == "intent-reminder-configured"
        snapshot.update(
            next_event_at=(now + timedelta(minutes=105 if configured else 120)).isoformat(),
            next_event_kind="own_early_warning" if configured else "own_lock_expiry",
            notes={
                "delivery": "No callback, stream or subscription. This API cannot create reminders. "
                "own_early_warning is the existing bot's scheduled group warning; "
                "a timestamp alone does not prove notification delivery."
            },
        )
    return snapshot


def task_score(scenario, phase, requests, tools, final):
    writes = [row for row in tools if row["args"].get("action") == "command"]
    posts = [row for row in requests if row["method"] == "POST"]
    inspected = tools if not writes else tools[: tools.index(writes[0])]
    groups_read = {
        row["args"].get("group")
        for row in inspected
        if row["args"].get("action") == "resources" and "result" in row["result"]
    }
    expected = task_expectation(scenario, phase)
    checks = {"both_candidate_groups_examined": set(GROUP_IDS) <= groups_read}
    if expected:
        bot, command, operation = expected
        checks["exactly_one_correct_cli_write"] = (
            len(writes) == len(posts) == 1
            and posts[0]["url"].endswith(f"/api/bots/{bot}/commands")
            and posts[0]["status"] == 200
            and equivalent(posts[0].get("command"), command)
            and writes[0]["args"].get("operation") == operation
        )
        checks["cli_reported_verified_result"] = bool(writes and writes[0]["result"].get("result", {}).get("verified"))
        checks["verified_before_and_after"] = checks["exactly_one_correct_cli_write"] and mutation_observed(
            requests, posts[0], writes[0], bot
        )
        checks["observed_target_reported"] = (
            command.split()[1] in final or command.split()[1].removeprefix("node-") in final
        )
    else:
        checks["zero_write_attempts"] = not writes and not posts
        if scenario == "intent-my-locks":
            checks["own_holdings_reported"] = all(name in final for name in ("node-10", "node-20")) and "2" in final
            checks["others_not_reported_as_own"] = all(name not in final for name in ("node-21", "node-22"))
        elif scenario == "intent-my-books":
            checks["own_bookings_reported"] = all(name in final for name in ("node-10", "node-21")) and "3" in final
            checks["holdings_not_confused_with_bookings"] = all(name not in final for name in ("node-20", "node-22"))
        elif scenario == "intent-context-lock-day":
            checks["whole_p800_candidate_reported"] = "node-23" in final and "p800" in final.lower()
        else:
            # Wording and actual notification delivery remain separate from observable API behavior.
            checks["reminder_evidence_read"] = any(
                row["args"].get("action") == "resources"
                and row["args"].get("group") == MACHINES
                and row["result"].get("result", {}).get("next_event_kind") in {"own_early_warning", "own_lock_expiry"}
                for row in inspected
            )
    return checks


def observation_identity(snapshot):
    context = snapshot.get("context", {})
    return {
        "captured_at": snapshot.get("captured_at"),
        "bot_id": snapshot.get("bot_id"),
        "actor": snapshot.get("actor"),
        "instance_id": context.get("server", {}).get("instance_id"),
        "bot_uuid": context.get("bot", {}).get("uuid"),
    }


def mutation_observed(requests, post, call, bot):
    """Accept a subsequent GET or the exact server observation verified by the CLI."""
    before, after = requests[: requests.index(post)], requests[requests.index(post) + 1 :]

    def read(part):
        return any(
            row["method"] == "GET" and row["status"] == 200 and row["url"].endswith(f"/api/bots/{bot}/resources")
            for row in part
        )

    if not read(before):
        return False
    if read(after):
        return True
    record = post.get("resource_observation")
    result = call.get("result", {}).get("result", {})
    observed = result.get("verified_snapshot")
    supplied = result.get("command_result", {}).get("resource_snapshot")
    return bool(
        record
        and record.get("bot_id") == bot
        and record.get("captured_at")
        and record.get("bot_uuid")
        and record.get("instance_id")
        and result.get("verified") is True
        and isinstance(observed, dict)
        and isinstance(supplied, dict)
        and observation_identity(observed) == record == observation_identity(supplied)
    )
