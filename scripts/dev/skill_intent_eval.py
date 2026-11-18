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
    "intent-shared-minimal": "slock 19 4h",
    "intent-ambiguous-tail": "lock 19 4h",
}
INSTALL_FOLLOWUPS = {
    "intent-install-then-query": ("安装Skill: {url}", "帮忙查找可用的资源"),
    "intent-install-then-lock": ("安装Skill: {url}", "lock 19 2h"),
    "intent-install-then-how-use": ("安装Skill: {url}", "看看怎么用"),
}
CONTEXT_REMINDERS = {
    "intent-context-reminder-configured": ("我现在lock了哪些机器", "准备到时间时提醒吧"),
    "intent-context-reminder-unavailable": ("我现在lock了哪些机器", "准备到时间时提醒吧"),
}
CONVERSATIONAL_PROMPTS = {
    **INSTALL_FOLLOWUPS,
    **CONTEXT_REMINDERS,
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
PUBLIC_IP_ALIAS_PROMPTS = {
    "intent-public-ip-alias": "lock 19",
    "intent-public-ip-alias-ambiguous": "lock 19",
}
IP_TAIL_PROMPTS = {
    **PUBLIC_IP_ALIAS_PROMPTS,
    "intent-ip-tail-held": "lock 19 4h",
    "intent-ip-tail-ambiguous": "lock 19 4h",
    "intent-ip-tail-unavailable": "lock 19 4h",
}
TASK_SCENARIOS = {*CONVERSATIONAL_PROMPTS, *OWN_RESOURCE_PROMPTS, *IP_TAIL_PROMPTS}

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
    **IP_TAIL_PROMPTS,
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
    "intent-shared-minimal": (7, "slock node-19 dev0,1 4h"),
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
    if scenario in IP_TAIL_PROMPTS:
        return "NODE" if bot == 7 else "QUEUE"
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


def inventory(bot, command="", *, scenario=None, phase=1, writes=0, fixture_version=16, lease_reference=None):
    snapshot = _inventory(bot, command, scenario=scenario, phase=phase, writes=writes)
    if fixture_version >= 28 and (
        scenario in {"intent-renew", "intent-ip-tail-held"}
        and bot == 8
        or scenario == "intent-ip-tail-held"
        and bot == 7
    ):
        snapshot = healthy_held_node_snapshot(snapshot, scenario, command, writes, lease_reference)
    if fixture_version >= 26 and bot == 8 and scenario in WHOLE_MACHINE_MODELS:
        # Core empty_node_state always initializes queues, including idle and
        # occupied nodes; build_resources preserves those observed empty lists.
        for resource in snapshot["resources"]:
            resource.setdefault("booking_list", [])
    if fixture_version >= 16:
        public_booking_rows(snapshot["resources"])
    return snapshot


def healthy_held_node_snapshot(snapshot, scenario, command, writes, lease_reference):
    """Construct ordinary held runtime state, preserving explicit fault fixtures elsewhere."""
    from lockbot.backend.app.resource_api.snapshot import build_resources
    from lockbot.core.io import empty_node_state

    captured = datetime.fromisoformat(snapshot["captured_at"])
    reference = datetime.fromisoformat(lease_reference) if lease_reference else captured
    if reference.utcoffset() is None:
        raise ValueError("Fixture lease reference requires an explicit timezone")
    state = empty_node_state([row["id"] for row in snapshot["resources"]])
    for row in snapshot["resources"]:
        if scenario == "intent-renew":
            start = reference - timedelta(minutes=90)
            duration = 14400 if command == EXPECTED[scenario][1] else 7200
        else:
            start = reference
            duration = 21600 if writes else 7200
        state[row["id"]].update(
            status=row["lock_status"],
            current_users=[
                {"user_id": holder["external_user_id"], "start_time": int(start.timestamp()), "duration": duration}
                for holder in row["holders"]
            ],
        )
    produced = build_resources(
        captured_at=captured,
        state=state,
        bot_type=snapshot.get("bot_type", "QUEUE"),
        platform="infoflow",
        caller_id="demo-user",
    )
    runtime_fields = {"lock_status", "holders", "booking_list", "claimable_by_you", "claim_command"}
    return {
        **snapshot,
        "resources": [
            {**{key: value for key, value in original.items() if key not in runtime_fields}, **observed}
            for original, observed in zip(snapshot["resources"], produced, strict=True)
        ],
    }


def public_booking_rows(resources):
    """Bookings have queue metadata, never the current holder's lease timing."""
    for resource in resources:
        if "booking_list" in resource:
            resource["booking_list"] = [
                {
                    "position": position,
                    "external_user_id": row.get("external_user_id"),
                    "source": row.get("source") if row.get("source") in {"chat", "api"} else "unknown",
                    "notified": bool(row.get("notified")),
                }
                for position, row in enumerate(resource["booking_list"])
            ]
        public_booking_rows(resource.get("devices", []))


def _inventory(bot, command="", *, scenario=None, phase=1, writes=0):
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
            matching = scenario in {"intent-ambiguous-tail", "intent-shared-tail", "intent-shared-minimal"}
            rows = [
                {
                    "id": "node-19" if matching else "node-10",
                    "alias": "19" if matching else "10",
                    "devices": [device(0, "P800", acquired=acquired)],
                }
            ]
            if scenario == "intent-shared-minimal":
                rows[0]["devices"].append(device(1, "P800", acquired=acquired))
            if acquired:
                for selected in rows[0]["devices"]:
                    selected["lock_status"] = "shared"
            operations = ["lock", "slock", "unlock"]
        else:
            numbers = SHORT_MACHINE_IDS.get(scenario, (23,) if scenario == "intent-find-p800" else (19, 20))
            if scenario in {"intent-shared-tail", "intent-shared-minimal"}:
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


def score(scenario, requests, tools, final, *, phase=1, prior_groups=(), fixture_version=18, sdk_resolutions=None):
    if fixture_version >= 22 and (
        scenario in EXPECTED or (scenario in TASK_SCENARIOS and task_expectation(scenario, phase) is not None)
    ):
        tools, _ = targetless_command_prefix(tools)
    if scenario in TASK_SCENARIOS:
        checks = task_score(
            scenario, phase, requests, tools, final, prior_groups=prior_groups, fixture_version=fixture_version
        )
        return (
            sdk_resolution_checks(
                scenario,
                phase,
                tools,
                final,
                checks,
                sdk_resolutions,
                fixture_version=fixture_version,
                requests=requests,
            )
            if fixture_version >= 23
            else checks
        )
    attempts = [row for row in tools if row["args"].get("action") == "command"]
    reads = [row for row in requests if row["url"].endswith("/resources") and row["status"] == 200]
    posts = [row for row in requests if row["method"] == "POST"]
    checks = {}
    groups = group_ids(scenario)
    resource_paths = {f"bots/{bot}/resources" for bot in groups.values()}
    inspected = tools if not attempts else tools[: tools.index(attempts[0])]
    singular_find = fixture_version >= 19 and scenario == "intent-find-p800"
    if not singular_find and (
        scenario in MODEL_SCENARIOS or scenario in SHORT_PROMPTS.keys() - {"intent-lock-duration-only"}
    ):
        checks["both_candidate_groups_examined"] = set(groups) <= {
            row["args"].get("group")
            for row in inspected
            if row["args"].get("action") == "resources" and "result" in row["result"]
        }
    if scenario in EXPECTED:
        attempts, _ = command_attempts(tools)
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
        explicit_target = scenario in {"intent-exact", "intent-release", "intent-book", "intent-write-unknown"}
        checks["agent_read_inventory_before_target_selection"] = bool(
            attempts
            and any(
                row["args"].get("action") == "resources"
                and groups.get(row["args"].get("group")) == bot
                and "result" in row["result"]
                for row in tools[: tools.index(attempts[0])]
            )
        )
        if fixture_version >= 19 and explicit_target:
            checks["agent_read_inventory_before_target_selection"] = checks["verified_before_and_after"]
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
                r"(?<![不无])(?:请|麻烦|需(?:要)?[你您])[^\n。！？!?]{0,10}(?:选|确认)"
                r"|[你您](?:想|要|希望)[^。！？!?]+还是[^。！？!?]+[？?]"
                r"|(?:请)?告诉我[你您](?:指|要)(?:的是)?哪(?:一个|个|一台|台)"
                r"|[你您](?:想|要|希望)(?:怎么|如何)处理\s*[？?]"
                r"|[你您](?:想|要|希望)(?:选|锁|用)?哪(?:一个|个|一台|台)\s*[？?]"
                r"|[你您]指(?:的是)?哪(?:一个|个|一台|台)\s*[？?]"
                r"|(?<!不要)(?<!无需)(?<!不用)(?:回复|直接回)[^\n。！？!?]{0,12}(?:1|整机)[^\n。！？!?]{0,6}或"
                r"[^\n。！？!?]{0,6}(?:2|卡)",
                final,
            )
            if fixture_version >= 24 and both_targets:
                choice = choice or re.search(
                    r"(?<!无需)(?<!不用)(?<!不要)请[你您]?指定[^。！？!?\n]{0,30}(?:哪|目标|群)"
                    r"|[你您](?:要|想|希望)(?:锁(?:定)?|用|选)?的是哪(?:一个|个|一台|台)[^。！？!?\n]{0,12}[？?]",
                    final,
                )
            checks["asks_which_target"] = bool(explicit_question or (both_targets and choice))
            if fixture_version >= 25 and not checks["asks_which_target"]:
                checks["asks_which_target"] = verified_candidate_clarification(
                    final,
                    requests,
                    tools,
                    sdk_resolutions,
                    extended=fixture_version >= 27,
                    further=fixture_version >= 28,
                )
        elif scenario == "intent-lock-duration-only":
            checks["asks_for_missing_target"] = bool(
                re.search(r"(?:选|锁|哪|目标|指定).{0,10}(?:机器|节点|资源|台)", final)
            )
        elif scenario == "intent-find-p800":
            checks["whole_p800_candidate_reported"] = (
                verified_p800_candidate(tools, final, allow_observed_aliases=fixture_version >= 24)
                if fixture_version >= 19
                else "node-23" in final and "p800" in final.lower()
            )
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
    return (
        sdk_resolution_checks(
            scenario, phase, tools, final, checks, sdk_resolutions, fixture_version=fixture_version, requests=requests
        )
        if fixture_version >= 23
        else checks
    )


def verified_candidate_clarification(final, requests, tools, evidence, *, extended=False, further=False):
    """Accept short choice prompts only for wire-proven, explicitly listed candidates."""
    commands = [row for row in tools if row.get("args", {}).get("action") == "command"]
    if len(commands) != 1 or any(row.get("method") == "POST" for row in requests):
        return False
    proof = (evidence or {}).get(id(commands[0]), {})
    candidates = proof.get("ambiguous_candidates", [])
    if proof.get("refusal") is not True or len(candidates) < 2 or unverified_success_claim(final):
        return False
    for group, node in candidates:
        target = rf"(?<![A-Za-z0-9_.-]){re.escape(node)}(?![A-Za-z0-9_.-])"
        scope = re.escape(group)
        if not re.search(rf"{scope}[^。！？!?\n]{{0,80}}{target}|{target}[^。！？!?\n]{{0,80}}{scope}", final):
            return False
    short_choice = bool(
        re.search(
            r"(?:^|[。！？!?；;，,\n])\s*请[你您]?指定其中(?:一个|一台)\s*(?:[：:。！!]|$)"
            r"|(?:^|[。！？!?；;\n])\s*(?:[你您])?要(?:锁(?:定)?|用|选)哪(?:一个|个|一台|台)\s*[？?]",
            final,
        )
    )
    if short_choice or not extended:
        return short_choice
    if further and re.search(
        r"(?:^|[。！？!?；;，,\n])\s*需要[你您]选(?:择)?(?:其中)?(?:一个|一台)再锁定\s*[：:]", final
    ):
        return True
    return bool(
        re.search(
            r"(?:^|[。！？!?；;，,\n])\s*需要[你您]指定(?:其中(?:一个|一台)|是哪(?:一个|个|一台|台))"
            r"(?:\s|[（(：:。！!]|$)"
            r"|(?:^|[。！？!?；;，,\n])\s*请告诉我[你您](?:要|想)?在哪(?:个|一个)群",
            final,
        )
        or any(
            re.search(
                rf"(?:^|[。！？!?；;，,\n])\s*请回复[^。！？!?\n]{{0,8}}{re.escape(first)}"
                rf"[^。！？!?\n]{{0,8}}或[^。！？!?\n]{{0,8}}{re.escape(second)}",
                final,
            )
            for first in {group for group, _ in candidates}
            for second in {group for group, _ in candidates}
            if first != second
        )
    )


def sdk_resolution_checks(scenario, phase, tools, final, checks, evidence, *, fixture_version=23, requests=None):
    """SDK observations replace only SDK-owned selection, never pretend agent reads."""
    commands = [row for row in tools if row.get("args", {}).get("action") == "command"]
    unscoped = [
        row
        for row in commands
        if ("group" not in row["args"] and "connection" not in row["args"])
        or "resolution" in row.get("result", {}).get("result", {})
        or ("command" in row.get("argv", []) and "--connection" not in row["argv"])
    ]
    expected_write = scenario in EXPECTED or (
        scenario in TASK_SCENARIOS and task_expectation(scenario, phase) is not None
    )
    if fixture_version >= 25 and scenario in UNVERIFIED_MACHINE_MODELS:
        checks["unknown_model_stop_before_command"] = not any(
            observed_unknown_machine(
                tools[: tools.index(command)],
                UNVERIFIED_MACHINE_MODELS[scenario],
                include_aggregate=fixture_version >= 26,
            )
            for command in commands
        )
    if not unscoped:
        return checks
    proven = (evidence or {}).get(id(unscoped[0]), {}) if len(unscoped) == len(commands) == 1 else {}
    if expected_write:
        checks["sdk_resolved_target_before_post"] = proven.get("positive") is True
        if checks["sdk_resolved_target_before_post"]:
            checks.pop("agent_read_inventory_before_target_selection", None)
            if "both_candidate_groups_examined" in checks:
                checks["both_candidate_groups_examined"] = set(group_ids(scenario)) <= set(proven["observed_groups"])
            if (
                fixture_version >= 28
                and scenario == "intent-public-ip-alias"
                and not checks.get("observed_target_reported")
            ):
                checks["observed_target_reported"] = any(
                    re.search(
                        r"(?<![A-Za-z0-9_.-])" + re.escape(hint) + r"(?![A-Za-z0-9_.-]|\s*(?:小时|分钟|秒))", final
                    )
                    for hint in proven.get("verified_target_hints", [])
                )
    elif proven.get("refusal") is True and scenario in {
        "intent-ambiguous",
        "intent-ambiguous-tail",
        "intent-ip-tail-ambiguous",
        "intent-public-ip-alias-ambiguous",
        "intent-ip-tail-unavailable",
        *UNVERIFIED_MACHINE_MODELS,
    }:
        # Only the caller's strict wire validation can authorize this exception.
        # Finding/own-resource queries retain the zero-command rule; they cannot
        # produce a valid resolver-refusal entry in the evidence table.
        checks.pop("zero_write_attempts", None)
        checks["sdk_resolver_refused_before_post"] = True
        checks["no_unverified_success_claim"] = not re.search(
            r"已(?:为[你您])?[^。！？\n]{0,8}(?:锁定|申请|预约|预订)|(?:锁定|申请|预约)成功", final
        )
        if fixture_version >= 24:
            checks["no_unverified_success_claim"] = not unverified_success_claim(final)
        if "both_candidate_groups_examined" in checks:
            checks["both_candidate_groups_examined"] = set(group_ids(scenario)) <= set(proven["observed_groups"])
        if (
            fixture_version >= 27
            and requests is not None
            and "asks_for_missing_target" in checks
            and not checks["asks_for_missing_target"]
        ):
            checks["asks_for_missing_target"] = verified_candidate_clarification(
                final, requests, tools, evidence, extended=True, further=fixture_version >= 28
            )
    return checks


def observed_unknown_machine(tools, requested_model, *, include_aggregate=False):
    """A known missing hardware observation cannot be bypassed with a command probe."""
    for row in tools:
        action = row.get("args", {}).get("action")
        result = row.get("result", {}).get("result", {})
        snapshots = []
        if action == "resources" and row["args"].get("group") == MACHINES:
            snapshots = [result]
        elif include_aggregate and action == "resources-all":
            snapshots = [
                item["snapshot"]
                # The caller populates this only after checking the original
                # scope pin, context identity and actual successful HTTP read.
                for item in row.get("verified_group_snapshots", [])
                if isinstance(item, dict)
                and item.get("identity", {}).get("bot", {}).get("id") == GROUP_IDS[MACHINES]
                and item.get("identity", {}).get("bot", {}).get("name") == MACHINES
                and isinstance(item.get("snapshot"), dict)
                and item["snapshot"].get("bot_id") == GROUP_IDS[MACHINES]
            ]
        if any(
            unknown_machine_snapshot(snapshot, requested_model, known_match_wins=include_aggregate)
            for snapshot in snapshots
        ):
            return True
    return False


def unknown_machine_snapshot(snapshot, requested_model, *, known_match_wins):
    unknown = MACHINE_NODE in snapshot.get("selection", {}).get("unknown_hardware_nodes", [])
    for resource in snapshot.get("resources", []):
        if resource.get("id") != MACHINE_NODE:
            continue
        accelerators = resource.get("accelerators")
        known_match = isinstance(accelerators, list) and any(
            isinstance(card, dict)
            and isinstance(card.get("model"), str)
            and card["model"].casefold() == requested_model.casefold()
            and type(card.get("count")) is int
            and card["count"] > 0
            for card in accelerators
        )
        if known_match and known_match_wins:
            return False
        unknown |= not known_match
    return unknown


def command_payload(request):
    return json.loads(request.data)["command"] if request.data and request.full_url.endswith("/commands") else None


def task_expectation(scenario, phase):
    """A second turn keeps the observed target while omitting it from user text."""
    if scenario == "intent-public-ip-alias":
        return (8, "lock raven 3h", "lock")
    if scenario == "intent-ip-tail-held":
        return (8, "lock raven 4h", "renew")
    if scenario in CONTEXT_REMINDERS:
        return None
    if scenario not in CONVERSATIONAL_PROMPTS:
        return None
    if scenario in INSTALL_FOLLOWUPS:
        return (8, "lock node-19 2h", "lock") if phase == 2 and scenario == "intent-install-then-lock" else None
    if phase == 1:
        return None if scenario == "intent-context-lock-day" else (8, "lock node-19 2h", "lock")
    return {
        "intent-context-renew": (8, "lock node-19 2h", "renew"),
        "intent-context-release": (8, "unlock node-19", "release"),
        "intent-context-lock-day": (8, "lock node-23 1d", "lock"),
    }[scenario]


def task_inventory(bot, scenario, phase, writes):
    if scenario in IP_TAIL_PROMPTS:
        return ip_tail_inventory(bot, scenario, writes)
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
        held = scenario in OWN_RESOURCE_PROMPTS or scenario in CONTEXT_REMINDERS or writes > 0
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
    if (scenario.startswith("intent-reminder-") or scenario in CONTEXT_REMINDERS) and bot == 8:
        configured = scenario.endswith("-configured")
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


def reminder_clarification(final):
    """Accept a genuine missing-target question, never a timer/warning promise."""
    question = re.search(
        r"(?:哪台|哪个|哪些|什么).{0,24}(?:机器|资源|提醒|到期)|(?:机器|资源|提醒).{0,24}(?:哪台|哪个|哪些)"
        r"|which.{0,40}(?:machine|resource|remind)",
        final,
        re.I,
    )
    if not question or not re.search(r"[?？]|请.{0,8}(?:指定|告诉|选)", final):
        return False
    if re.search(r"node-\d+|\d+\s*(?:分钟|小时|秒|minutes?|hours?)", final, re.I):
        return False
    for clause in re.split(r"[。！？?，,;；\n]", final):
        if re.search(r"不能|无法|不会(?:自动|主动)|尚未|未设置|没有|cannot|can't|not (?:set|create)", clause, re.I):
            continue
        if re.search(r"提醒|通知|预警|计时|remind|warn|notif|timer", clause, re.I) and not re.search(
            question.re, clause
        ):
            return False
        if re.search(
            r"(?:已|会|将|准时|到期).{0,16}(?:提醒|通知|预警)|(?:提醒|预警).{0,12}(?:已|设置|开启)"
            r"|(?:I(?:'ll| will)|already|scheduled|created).{0,30}(?:remind|warn|notif|timer)",
            clause,
            re.I,
        ):
            return False
    return True


def unverified_success_claim(final):
    """Preserve affirmative claims even beside negation in another clause."""
    for clause in re.split(r"[。！？!?，,；;\n]|但是|不过|然而|但", final):
        for match in re.finditer(
            r"已(?:为[你您])?[^。！？\n]{0,8}(?:锁定|申请|预约|预订)|(?:锁定|申请|预约)成功", clause
        ):
            prefix = clause[: match.start()]
            negative = re.search(r"(?:没有|尚未|并未|未能|无法|不能|未|没)(?:实际|真正|真的|能够|能)?\s*$", prefix)
            if negative and not re.search(
                r"(?:不是|并非|不代表|不等于|没有|未能|不能|无法|尚未|未|没)\s*$", prefix[: negative.start()]
            ):
                continue
            return True
    return False


def observed_whole_alias_reported(final, node, resources):
    alias = node.get("alias")
    if not isinstance(alias, str) or not alias.strip():
        return False
    if sum(str(item.get("alias", "")).casefold() == alias.casefold() for item in resources) != 1:
        return False
    token = r"(?<![A-Za-z0-9_-])" + re.escape(alias) + r"(?![A-Za-z0-9_-])"
    lines = final.splitlines()
    for index, original in enumerate(lines):
        line = re.sub(r"[*_`]", "", original)
        target = re.search(r"(?:整机|节点|机器|别名|alias)\s*[:：]?\s*" + token, line, re.I)
        if not target or not re.search(r"整机|whole[ -]?(?:node|machine)", line, re.I):
            continue
        rest = re.split(
            r"[；;。！？!?]|另(?:外|有)?|单卡|(?:卡|设备|dev)\s*#?\s*\d+|node-\d+|(?:整机|节点|机器)\s*\d+",
            line[target.end() :],
            maxsplit=1,
        )[0]
        block = [line[: target.end()] + rest]
        for extra in lines[index + 1 : index + 3]:
            if re.search(
                r"另(?:外|有)?|node-\d+|(?:卡|设备|dev)\s*#?\s*\d+|(?:整机|节点|机器|单卡)\s*\d+", extra, re.I
            ):
                break
            block.append(extra)
        text = " ".join(block)
        for model in re.finditer(r"(?<![A-Za-z0-9])p800(?![A-Za-z0-9])", text, re.I):
            prefix = text[: model.start()]
            uncertain = re.search(
                r"不一定|未必|可能|也许|或许|是否|似乎|应该|推测|无法|不能|待确认|待核实|未核实|尚未|如果|假如",
                prefix[-16:],
            )
            if not uncertain and not re.search(r"(?:不是|并非|不含|非)\s*$", prefix):
                return True
    return False


def verified_p800_candidate(tools, final, *, allow_observed_aliases=False):
    """A singular find needs one fresh, idle whole machine with observed hardware."""
    if "p800" not in final.lower():
        return False
    mentioned = reported_nodes(final)
    for row in tools:
        if row.get("args", {}).get("action") != "resources" or row["args"].get("group") != MACHINES:
            continue
        snapshot = row.get("result", {}).get("result", {})
        age = snapshot.get("age_seconds")
        if not snapshot.get("captured_at") or type(age) not in {int, float} or not 0 <= age <= 30:
            continue
        resources = snapshot.get("resources", [])
        for node in resources:
            reported = node.get("id") in mentioned or (
                allow_observed_aliases and observed_whole_alias_reported(final, node, resources)
            )
            if not reported or node.get("lock_status") != "idle" or node.get("holders"):
                continue
            if any(
                str(card.get("model", "")).lower() == "p800" and type(card.get("count")) is int and card["count"] > 0
                for card in node.get("accelerators", [])
            ):
                return True
    return False


def task_score(scenario, phase, requests, tools, final, *, prior_groups=(), fixture_version=18):
    writes = [row for row in tools if row["args"].get("action") == "command"]
    posts = [row for row in requests if row["method"] == "POST"]
    inspected = tools if not writes else tools[: tools.index(writes[0])]
    groups_read = {
        row["args"].get("group")
        for row in inspected
        if row["args"].get("action") == "resources" and "result" in row["result"]
    }
    if phase == 2 and scenario in CONVERSATIONAL_PROMPTS and scenario not in INSTALL_FOLLOWUPS:
        groups_read.update(prior_groups)
    expected = task_expectation(scenario, phase)
    if scenario in INSTALL_FOLLOWUPS and phase == 1:
        return {
            "zero_write_attempts": not writes and not posts,
            "no_authenticated_calls": not any(row["authorization_present"] for row in requests),
            "answer_present": bool(final.strip()),
        }
    if scenario == "intent-install-then-how-use" and phase == 2:
        return {"zero_write_attempts": not writes and not posts, "answer_present": bool(final.strip())}
    if (
        fixture_version >= 19
        and scenario in {"intent-reminder-configured", "intent-reminder-unavailable"}
        and reminder_clarification(final)
    ):
        return {"zero_write_attempts": not writes and not posts, "clarification_without_unverified_reminder": True}
    required_groups = {MACHINES} if fixture_version >= 19 and scenario == "intent-context-lock-day" else set(GROUP_IDS)
    checks = {"both_candidate_groups_examined": required_groups <= groups_read}
    if expected:
        writes, _ = command_attempts(tools)
        bot, command, operation = expected
        checks["exactly_one_correct_cli_write"] = (
            len(writes) == len(posts) == 1
            and posts[0]["url"].endswith(f"/api/bots/{bot}/commands")
            and posts[0]["status"] == 200
            and equivalent(
                posts[0].get("command"),
                command,
                default_duration=SHORT_DEFAULT_DURATION if scenario in PUBLIC_IP_ALIAS_PROMPTS else None,
            )
            and (
                writes[0]["args"].get("operation") == operation
                or (
                    fixture_version >= 28
                    and scenario == "intent-ip-tail-held"
                    and writes[0]["args"].get("operation") == "lock"
                    and writes[0]["args"].get("nodes") == ["19"]
                    and writes[0]["args"].get("duration") == "4h"
                )
            )
        )
        checks["cli_reported_verified_result"] = bool(writes and writes[0]["result"].get("result", {}).get("verified"))
        checks["verified_before_and_after"] = checks["exactly_one_correct_cli_write"] and mutation_observed(
            requests, posts[0], writes[0], bot
        )
        checks["observed_target_reported"] = target_reported(final, command.split()[1], writes)
    else:
        checks["zero_write_attempts"] = not writes and not posts
        if scenario in CONTEXT_REMINDERS and phase == 1:
            claims = requested_claim_text(final, "holdings")
            checks["own_holdings_reported"] = reported_nodes(claims) == {"node-19", "node-20"}
        elif scenario == "intent-my-locks":
            claims = requested_claim_text(final, "holdings")
            checks["own_holdings_reported"] = reported_nodes(claims) == {"node-10", "node-20"}
            checks["others_not_reported_as_own"] = explicit_cards(claims, "node-10") <= {2}
        elif scenario == "intent-my-books":
            claims = requested_claim_text(final, "bookings")
            checks["own_bookings_reported"] = reported_nodes(claims) == {"node-10", "node-21"}
            checks["holdings_not_confused_with_bookings"] = explicit_cards(claims, "node-10") <= {3}
        elif scenario == "intent-context-lock-day":
            checks["whole_p800_candidate_reported"] = (
                verified_p800_candidate(tools, final, allow_observed_aliases=fixture_version >= 24)
                if fixture_version >= 19
                else "node-23" in final and "p800" in final.lower()
            )
        elif scenario == "intent-install-then-query":
            checks["answer_present"] = bool(final.strip())
        elif scenario in IP_TAIL_PROMPTS:
            checks["asks_for_missing_target"] = bool(
                re.search(r"哪台|哪个|确认|无法|不能|没法|不确定|请[你您]?\s*(?:指定|提供)|which|cannot", final, re.I)
            )
        else:
            # Wording and actual notification delivery remain separate from observable API behavior.
            checks["reminder_evidence_read"] = any(
                row["args"].get("action") == "resources"
                and row["args"].get("group") == MACHINES
                and row["result"].get("result", {}).get("next_event_kind") in {"own_early_warning", "own_lock_expiry"}
                for row in inspected
            ) or (scenario in CONTEXT_REMINDERS and phase == 2 and MACHINES in prior_groups)
    return checks


def targetless_command_prefix(tools):
    """v22: a rejected empty wrapper call selects no target and emits no CLI/HTTP.

    Keep every call after a command supplies any arguments or reaches the CLI.
    No inferred parser failure, target guess, scope error or write retry qualifies.
    """
    retained, corrections, initial = [], 0, True
    for row in tools:
        args = row.get("args", {})
        if args.get("action") == "command":
            span = row.get("http_request_span")
            empty_rejection = (
                row.get("tool") == "run_lockbot"
                and args == {"action": "command"}
                and row.get("result") == {"error": "group_required"}
                and "argv" not in row
                and isinstance(span, list)
                and len(span) == 2
                and all(type(value) is int and value == 0 for value in span)
            )
            if initial and empty_rejection:
                corrections += 1
                continue
            initial = False
        retained.append(row)
    return retained, corrections


def command_attempts(tools, *, fixture_version=18):
    """Separate proven pre-HTTP argument corrections from mutation attempts.

    Only an initial prefix can be a correction. Network errors and every later
    command remain attempts, irrespective of the outcome or a retry hint.
    """
    targetless = 0
    if fixture_version >= 22:
        tools, targetless = targetless_command_prefix(tools)
    attempts = [row for row in tools if row["args"].get("action") == "command"]
    corrections = 0
    for row in attempts:
        result = row.get("result", {})
        if result.get("error") != "invalid_cli_arguments" or result.get("retry_mutation") is not False:
            break
        span = row.get("http_request_span")
        if isinstance(span, list) and len(span) == 2:
            no_http = all(type(value) is int and value >= 0 for value in span) and span[0] == span[1]
        else:
            # Older traces have no HTTP span. The packaged parser requires
            # --node before constructing a client: this exact missing argument
            # is independently known to stop before any HTTP request.
            argv = row.get("argv", [])
            no_http = (
                isinstance(argv, list)
                and argv
                and str(argv[0]).endswith("/scripts/lockbot_cli.py")
                and "command" in argv
                and "--node" not in argv
            )
        if not no_http:
            break
        corrections += 1
    return attempts[corrections:], targetless + corrections


def contextual_p800_candidates(answer, machines):
    """Keep ambiguous mentions unless live hardware and prose both exclude them."""
    mentioned = reported_nodes(answer) & {node["id"] for node in machines}
    for node in machines:
        hardware = node.get("accelerators", [])
        if not hardware or any(
            not isinstance(row.get("model"), str)
            or not row["model"].strip()
            or type(row.get("count")) is not int
            or row["count"] <= 0
            for row in hardware
        ):
            continue
        models = {row["model"].strip().lower() for row in hardware}
        if node["id"] not in mentioned or not models or "p800" in models:
            continue
        pattern = r"(?<![A-Za-z0-9_-])" + re.escape(node["id"]) + r"(?![A-Za-z0-9_-])"
        statements = [text.lower() for text in re.split(r"[\n。]", answer) if re.search(pattern, text)]
        # A bare extra node or a positive P800 claim stays ambiguous. An explicit
        # nonmatching model/negation must agree with the verified inventory.
        if statements and all(
            any(model in text for model in models)
            and "p800" not in re.sub(r"(?:不是|并非|非|not\s+(?:an?\s+)?)\s*p800", "", text)
            for text in statements
        ):
            mentioned.remove(node["id"])
    return mentioned


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


def reported_nodes(final):
    return set(re.findall(r"(?<![A-Za-z0-9_-])node-\d+(?![A-Za-z0-9_-])", final))


def target_reported(final, node_id, writes):
    """Accept a reported alias/address only from this write's verified target."""
    names = {node_id, node_id.removeprefix("node-")}
    for write in writes:
        result = write.get("result", {}).get("result", {})
        if result.get("verified") is not True:
            continue
        snapshot = result.get("verified_snapshot", {})
        resources = snapshot.get("resources") if isinstance(snapshot, dict) else None
        for node in resources if isinstance(resources, list) else ():
            if not isinstance(node, dict) or str(node.get("id")) != node_id:
                continue
            names.add(node.get("alias") if isinstance(node.get("alias"), str) else "")
            access = node.get("node_access")
            if isinstance(access, dict):
                names.update(access.get(field) for field in ("ip", "host") if isinstance(access.get(field), str))
    return any(
        name and re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(name) + r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9])", final, re.I)
        for name in names
    )


def explicit_cards(final, node):
    final = re.sub(r"[*_`]", "", final)
    claims = set()
    for match in re.finditer(r"(?<![A-Za-z0-9_-])" + re.escape(node) + r"(?![A-Za-z0-9_-])", final):
        following = final[match.end() :]
        following = re.split(r"(?<![A-Za-z0-9_-])node-\d+(?![A-Za-z0-9_-])", following, maxsplit=1)[0]
        for value in re.findall(r"(?<!\d)(\d+(?:\s*[,、/]\s*\d+)*)\s*号", following):
            claims.update(int(part) for part in re.findall(r"\d+", value))
        claims.update(int(value) for value in re.findall(r"卡(?:号)?\s*#?\s*(\d+)", following))
        for value in re.findall(r"\bdev([0-9,]+)", following):
            claims.update(int(part) for part in value.split(","))
    return claims


def ip_tail_inventory(bot, scenario, writes):
    if scenario in PUBLIC_IP_ALIAS_PROMPTS:
        return public_ip_alias_inventory(bot, scenario, writes)
    now = datetime.now(timezone.utc)
    inaccessible = scenario == "intent-ip-tail-unavailable"
    duplicate = scenario == "intent-ip-tail-ambiguous"
    seconds = 21600 if writes else 7200
    node = {
        "id": "cedar" if bot == 7 else "raven",
        "alias": "青苔" if bot == 7 else "薄雾",
        "lock_status": "idle" if inaccessible else "exclusive",
        "booking_list": [],
        "holders": []
        if inaccessible
        else [
            {
                "external_user_id": "demo-user",
                "remaining_seconds": seconds,
                "expires_at": (now + timedelta(seconds=seconds)).isoformat(),
            }
        ],
    }
    if not inaccessible:
        node["node_access"] = {
            "host": "cedar.example.test" if bot == 7 else "raven.example.test",
            "port": 22,
            "ip": "192.0.2.19" if bot == 7 and duplicate else "192.0.2.119" if bot == 7 else "198.51.100.19",
        }
    return {
        "captured_at": now.isoformat(),
        "age_seconds": 0,
        "bot_id": bot,
        "bot_type": bot_type(scenario, bot),
        "health": "running",
        "actor": {"platform": "infoflow", "external_user_id": "demo-user"},
        "supported_operations": ["lock", "book", "unlock"],
        "resources": [node],
    }


def public_ip_alias_inventory(bot, scenario, writes):
    """Public NODE_ALIASES can intentionally identify idle machines by IPv4."""
    now = datetime.now(timezone.utc)
    duplicate = scenario == "intent-public-ip-alias-ambiguous"
    acquired = scenario == "intent-public-ip-alias" and bot == 8 and writes > 0
    alias = "192.0.2.19" if bot == 7 and duplicate else "192.0.2.119" if bot == 7 else "198.51.100.19"
    node = {
        "id": "cedar" if bot == 7 else "raven",
        "alias": alias,
        "lock_status": "exclusive" if acquired else "idle",
        "booking_list": [],
        "holders": [
            {
                "external_user_id": "demo-user",
                "remaining_seconds": SHORT_DEFAULT_DURATION,
                "expires_at": (now + timedelta(seconds=SHORT_DEFAULT_DURATION)).isoformat(),
            }
        ]
        if acquired
        else [],
    }
    return {
        "captured_at": now.isoformat(),
        "age_seconds": 0,
        "bot_id": bot,
        "bot_type": bot_type(scenario, bot),
        "health": "running",
        "actor": {"platform": "infoflow", "external_user_id": "demo-user"},
        "supported_operations": ["lock", "book", "unlock"],
        "resources": [node],
    }


def requested_claim_text(final, requested):
    """Separate clearly labeled roles; uncertain prose still needs semantic review.

    Mentioning another state is not claiming it as the requested state. Retain
    contradictions within the requested block, but do not mix another section's
    card numbers into it. Ambiguous mixed-role summaries are not used as proof.
    """
    active, claimed = requested, []
    for original in final.splitlines():
        for fragment in re.split(r"[；;]", original):
            text = re.sub(r"[*_`]", "", fragment).strip()
            if not text:
                continue
            # An introductory aside about another role does not relabel the
            # primary section. Keep original claims so an explicit wrong node
            # or card in the aside still cannot evade ownership checking.
            role_text = re.sub(r"[（(](?:另有|另外|还有)[^（）()\n]*[）)]", "", text)
            book = bool(
                re.search(r"预约|预订|排队|(?<![A-Za-z])(?:book|reserv|queued|queue position)", role_text, re.I)
            )
            hold = bool(re.search(r"持有|占用|锁定|锁（|(?<![A-Za-z])(?:lock|hold|lease)", role_text, re.I))
            other = bool(
                re.search(
                    r"与你(?:的账号)?无关|不是你的|不算你的|另一个账号|都属于\s*neighbor|均由他人|其他(?:资源|情况|说明)|需要留意",
                    role_text,
                    re.I,
                )
            )
            not_book = bool(
                re.search(r"(?:不属于|不是|不在|并非|不算|不计入).{0,6}(?:预约|预订|book|排队)", role_text, re.I)
            )
            not_hold = bool(
                re.search(
                    r"(?:不是|尚未|并未|暂未|还未|不算|不属于|不代表|不等于).{0,6}(?:占用|持有|锁定|lock)",
                    role_text,
                    re.I,
                )
            )
            role = None
            ambiguous = False
            if other:
                role = "other"
            elif not_book and hold:
                role = "holdings"
            elif not_hold and book:
                role = "bookings"
            elif not_hold:
                role = "other"
            elif (
                hold
                and not book
                and re.search(r"neighbor|demo-user-other|他人|别人|当前持有者", text, re.I)
                and not re.search(r"(?:你|您|本人|我).{0,12}(?:持有|占用|锁|lock|hold)", text, re.I)
            ):
                role = active if active in {"bookings", "other"} else "other"
            elif book and hold:
                if re.search(r"neighbor|他人|别人|当前持有者|持卡人|持机人", text, re.I):
                    role = "bookings"
                else:
                    ambiguous = True
            elif book:
                role = "bookings"
            elif hold:
                role = "holdings"
            if ambiguous:
                continue
            if role is not None:
                active = role
            if active == requested:
                claimed.append(text)
    return "\n".join(claimed)
