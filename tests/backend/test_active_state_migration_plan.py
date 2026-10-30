"""Disabled active-state planning preserves allocator incarnations and exact principal bounds."""

import copy
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from lockbot.backend.app.bot_migrations.active_state import (
    MappingScope,
    ReviewedLink,
    build_active_state_plan,
    validate_active_state_plan,
)
from lockbot.backend.app.bot_migrations.coordinator import MigrationBlocked

NOW = 1000
SCOPE = MappingScope("instance", "a" * 32, "bot-uuid", 3, 7, 8, "cli_fixture", "tenant", "oc_fixture")


def bot(kind="QUEUE"):
    return SimpleNamespace(
        uuid="bot-uuid",
        identity_revision=3,
        user_id=7,
        platform="Infoflow",
        bot_type=kind,
        cluster_configs='["node1", "node2"]'
        if kind in {"NODE", "QUEUE"}
        else '{"node1":["gpu","gpu"],"node2":["gpu"]}',
    )


def link(source, target, **changes):
    return replace(
        ReviewedLink(
            SCOPE,
            source,
            target,
            hashlib.sha256(("source:" + source).encode()).hexdigest()[:32],
            hashlib.sha256(("target:" + target).encode()).hexdigest()[:32],
            9,
            NOW + 250,
        ),
        **changes,
    )


def user(name, start=900, duration=500, *, offer=False, serial="1"):
    return {
        "user_id": name,
        "start_time": start,
        "duration": duration,
        "holding_id": serial * 32,
        "is_notified": offer,
        "source": "api",
    }


def entry(holders=(), books=()):
    return {"status": "exclusive" if holders else "idle", "current_users": list(holders), "booking_list": list(books)}


def state():
    return {
        "node1": entry([user("alice")], [user("bob", serial="2"), user("carol", serial="3")]),
        "node2": entry([], [user("bob", start=980, offer=True, serial="4"), user("alice", serial="5")]),
    }


def links():
    return [link("alice", "ou_alice"), link("bob", "ou_bob"), link("carol", "ou_carol")]


def build(value=None, principals=None, resource_bot=None, rules=None):
    return build_active_state_plan(
        state() if value is None else value,
        resource_bot or bot(),
        rules or {},
        links() if principals is None else principals,
        SCOPE,
        reviewed_by=9,
        now=NOW,
    )


def test_lock_book_offer_times_order_and_incarnations_are_exactly_preserved():
    original = state()
    plan = build(original)
    expected = copy.deepcopy(original)
    for node in expected.values():
        for kind in ("current_users", "booking_list"):
            for actor in node[kind]:
                actor["user_id"] = "ou_" + actor["user_id"]
    assert plan.mapped_state == expected
    assert original == state(), "planner changed live source data"
    assert plan.valid_before == 1250
    assert plan.counts == {"resources": 2, "holders": 1, "bookings": 4, "offers": 1}
    assert plan.provenance[0][4:6] == ("alice", "ou_alice")
    summary = validate_active_state_plan(plan, original, bot(), {}, SCOPE, reviewed_by=9, now=1100)
    assert not summary["can_commit"] and not summary["source_credentials_transferred"]
    assert all(value not in str(summary) for value in ("alice", "ou_bob", "holding_id"))


@pytest.mark.parametrize("kind", ["NODE", "DEVICE", "QUEUE_DEVICE"])
def test_node_and_card_resource_granularity_is_preserved(kind):
    source = {"node1": entry([user("alice")]), "node2": entry([user("bob", serial="2")])}
    if kind != "NODE":
        source = {
            "node1": [
                dict(entry([user("alice")]), dev_id=0, model="gpu"),
                dict(entry([user("alice", serial="3")]), dev_id=1, model="gpu"),
            ],
            "node2": [dict(entry([user("bob", serial="2")]), dev_id=0, model="gpu")],
        }
    plan = build(source, links()[:2], bot(kind))
    assert plan.counts["resources"] == (2 if kind == "NODE" else 3)
    assert plan.mapped_state.keys() == source.keys()
    if kind != "NODE":
        assert [card["dev_id"] for card in plan.mapped_state["node1"]] == [0, 1]
        with pytest.raises(MigrationBlocked, match="quota"):
            build(source, links()[:2], bot(kind), {"MAX_RESOURCES_PER_USER": 1})


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate-source",
        "merged-target",
        "wrong-name",
        "unverified",
        "wrong-scope",
        "wrong-reviewer",
        "expired-proof",
        "unused",
    ],
)
def test_mapping_is_explicit_bijective_reviewed_and_scoped(change):
    mappings = links()
    if change == "missing":
        mappings.pop()
    elif change == "duplicate-source":
        mappings.append(link("alice", "ou_another"))
    elif change == "merged-target":
        mappings[1] = link("bob", "ou_alice")
    elif change == "wrong-name":
        mappings[0] = link("Alice", "ou_alice")
    elif change == "unverified":
        mappings[0] = link("alice", "ou_alice", target_group_sender_proof="")
    elif change == "wrong-scope":
        mappings[0] = link("alice", "ou_alice", scope=replace(SCOPE, target_tenant_key="other"))
    elif change == "wrong-reviewer":
        mappings[0] = link("alice", "ou_alice", reviewed_by=10)
    elif change == "expired-proof":
        mappings[0] = link("alice", "ou_alice", expires_at=999)
    elif change == "unused":
        mappings.append(link("unused", "ou_unused"))
    with pytest.raises(MigrationBlocked):
        build(principals=mappings)


@pytest.mark.parametrize("kind", ["holder", "offer"])
def test_expired_obligations_are_not_deleted_or_renewed(kind):
    source = state()
    actor = source["node1"]["current_users"][0] if kind == "holder" else source["node2"]["booking_list"][0]
    actor["start_time"] = 0
    before = copy.deepcopy(source)
    with pytest.raises(MigrationBlocked, match="reconciliation"):
        build(source)
    assert source == before


def test_normal_booking_time_is_not_an_expiration_and_offer_timer_is_not_restarted():
    source = state()
    source["node1"]["booking_list"][0].update(start_time=0, duration=1)
    source["node2"]["booking_list"][0]["start_time"] = 701
    plan = build(source)
    assert plan.valid_before == 1001
    assert plan.mapped_state["node2"]["booking_list"][0]["start_time"] == 701
    with pytest.raises(MigrationBlocked, match="expired"):
        validate_active_state_plan(plan, source, bot(), {}, SCOPE, reviewed_by=9, now=1001)


def test_identity_whitelist_is_explicitly_mapped_without_changing_neutral_policy():
    rules = {"DURATION_WHITELIST": ["alice", "bob"], "FORBID_RELOCK": True, "MAX_RESOURCES_PER_USER": 2}
    plan = build(rules=rules)
    assert plan.mapped_rules == dict(rules, DURATION_WHITELIST=["ou_alice", "ou_bob"])
    assert rules["DURATION_WHITELIST"] == ["alice", "bob"]
    with pytest.raises(MigrationBlocked, match="unmapped"):
        build(rules={"DURATION_WHITELIST": ["not_allocated"]})
    with pytest.raises(MigrationBlocked, match="review"):
        build(rules={"ALLOWED_GROUP_IDS": ["source-group"]})


@pytest.mark.parametrize("change", ["state", "rules", "target", "owner", "revision", "scope", "reviewer"])
def test_changed_plan_requires_new_review(change):
    plan, source, resource_bot, rules, scope, reviewer = build(), state(), bot(), {}, SCOPE, 9
    if change == "state":
        source["node1"]["current_users"][0]["duration"] += 1
    elif change == "rules":
        rules["FORBID_RELOCK"] = True
    elif change == "target":
        plan.mapped_state["node1"]["current_users"][0]["duration"] += 1
    elif change == "owner":
        resource_bot.user_id = 10
    elif change == "revision":
        resource_bot.identity_revision += 1
    elif change == "scope":
        scope = replace(SCOPE, target_chat_id="oc_other")
    else:
        reviewer = 10
    with pytest.raises(MigrationBlocked, match="changed"):
        validate_active_state_plan(plan, source, resource_bot, rules, scope, reviewed_by=reviewer, now=1100)


def test_private_metadata_is_not_silently_transferred_under_old_authority():
    source = state()
    source["node1"]["current_users"][0]["usage"] = {"purpose": "private fixture"}
    with pytest.raises(MigrationBlocked, match="metadata_requires_explicit_review"):
        build(source)


@pytest.mark.parametrize(
    "field,value",
    [
        ("start_time", float("nan")),
        ("start_time", 1001),
        ("duration", True),
        ("duration", -1),
        ("holding_id", "legacy"),
    ],
)
def test_invalid_allocation_incarnation_is_not_normalized(field, value):
    source = state()
    source["node1"]["current_users"][0][field] = value
    with pytest.raises(MigrationBlocked, match="incarnation"):
        build(source)


def test_freed_then_reacquired_allocation_cannot_reuse_old_comparison_plan():
    source = state()
    plan = build(source)
    source["node1"]["current_users"][0]["holding_id"] = "f" * 32
    with pytest.raises(MigrationBlocked, match="changed"):
        validate_active_state_plan(plan, source, bot(), {}, SCOPE, reviewed_by=9, now=1100)


def test_unverified_none_proof_is_rejected_with_safe_code():
    principals = links()
    principals[0] = link("alice", "ou_alice", source_account_proof=None)
    with pytest.raises(MigrationBlocked, match="proof_unverified"):
        build(principals=principals)


def test_proof_reference_cannot_be_reused_for_another_principal():
    principals = links()
    principals[1] = replace(principals[1], target_group_sender_proof=principals[0].target_group_sender_proof)
    with pytest.raises(MigrationBlocked, match="proof_reused"):
        build(principals=principals)


@pytest.mark.parametrize("change", ["inventory", "bot_type"])
def test_reviewed_resource_identity_cannot_change_without_new_plan(change):
    source, resource_bot = state(), bot()
    plan = build(source, resource_bot=resource_bot)
    if change == "inventory":
        resource_bot.cluster_configs = '["node1"]'
    else:
        resource_bot.bot_type = "NODE"
    with pytest.raises(MigrationBlocked, match="changed"):
        validate_active_state_plan(plan, source, resource_bot, {}, SCOPE, reviewed_by=9, now=1100)


def test_inventory_digest_uses_canonical_json_not_formatting():
    source, resource_bot = state(), bot()
    plan = build(source, resource_bot=resource_bot)
    resource_bot.cluster_configs = '[ "node1",  "node2" ]'
    assert (
        validate_active_state_plan(plan, source, resource_bot, {}, SCOPE, reviewed_by=9, now=1100)["can_commit"]
        is False
    )
