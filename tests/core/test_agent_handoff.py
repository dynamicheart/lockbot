"""Exercise parent -> JSON handoff -> final payload without sending any commands."""

import copy
import json

import pytest
from lockbot.agent_client.handoff import (
    GateError,
    feedback_url,
    final_command_payload,
    lease_matches,
    make_handoff,
    scope_from_authenticated_context,
)

ORIGIN = "https://resources.example.invalid"
REFERENCE = "keyring:resource-group-a"
COMMAND = "lock node-a 30m"


@pytest.fixture
def context():
    return {
        "server": {"service_id": "lockbot.resource-api", "instance_id": "instance-a", "api_version": "1"},
        "bot": {"id": 7, "uuid": "bot-a"},
        "actor": {"platform": "Infoflow", "external_user_id": "user-a"},
        "credential_scope": "one_user_one_bot",
        "agent_access_enabled": True,
        "usage_privacy": {
            "private_fields": ["purpose", "agent_label"],
            "legacy_purpose_visibility": "private",
            "public_task_categories": {"unit_testing": "Unit testing", "model_load_testing": "Model load testing"},
        },
    }


def envelope(context, mode="command_only"):
    return make_handoff(ORIGIN, context, REFERENCE, {"version": 1, "mode": mode})


def final(context, trusted, candidate, child=None, **changes):
    arguments = {
        "origin": ORIGIN,
        "authenticated_context": context,
        "credential_ref": REFERENCE,
        "candidate": candidate,
        **changes,
    }
    received = json.loads(json.dumps(trusted)) if child is None else child
    return final_command_payload(trusted, received, **arguments)


def test_serialized_handoff_keeps_policy_and_final_gate_strips_child_fields(context):
    trusted = make_handoff(ORIGIN, context, REFERENCE)
    candidate = {
        "command": COMMAND,
        "purpose": "internal research detail",
        "agent_label": "untrusted-child",
        "task_category": "unit_testing",
        "user_id": "another-user",
        "Authorization": "untrusted-secret",
        "identity": {"admin": True},
    }
    before = copy.deepcopy(candidate)
    assert final(context, trusted, candidate) == {"command": COMMAND}
    assert candidate == before
    assert trusted["privacy_policy"] == {"version": 1, "mode": "command_only"}
    assert trusted["scope"]["credential_ref"] == REFERENCE
    assert set(trusted) == {"schema_version", "scope", "privacy_policy"}


def test_public_category_only_uses_authenticated_preset_ids(context):
    trusted = envelope(context, "public_category")
    candidate = {"command": COMMAND, "task_category": "unit_testing", "purpose": "internal", "agent_label": "child"}
    assert final(context, trusted, candidate) == {"command": COMMAND, "task_category": "unit_testing"}
    for invalid in ("Unit testing", "unknown", ["unit_testing"], {"unit_testing": "malicious"}):
        with pytest.raises(GateError):
            final(context, trusted, {**candidate, "task_category": invalid})
    assert final(context, trusted, {"command": COMMAND}) == {"command": COMMAND}


def test_private_details_require_explicit_policy_and_server_support(context):
    candidate = {
        "command": COMMAND,
        "purpose": "short private note",
        "agent_label": "client",
        "task_category": "unit_testing",
    }
    trusted = envelope(context, "private_details")
    assert final(context, trusted, candidate) == {
        "command": COMMAND,
        "purpose": "short private note",
        "agent_label": "client",
    }
    for privacy in (None, {}, {"private_fields": ["purpose", "agent_label"]}, {"legacy_purpose_visibility": "private"}):
        old = {**context, "usage_privacy": privacy}
        assert final(old, trusted, candidate) == {"command": COMMAND}
    for field, value in (("purpose", "x" * 513), ("agent_label", "a\nforged"), ("purpose", [])):
        with pytest.raises(GateError):
            final(context, trusted, {**candidate, field: value})


def test_old_server_without_privacy_capability_downgrades_to_command_only(context):
    for mode in ("command_only", "public_category", "private_details"):
        trusted = envelope(context, mode)
        old = {key: value for key, value in context.items() if key != "usage_privacy"}
        assert final(old, trusted, {"command": COMMAND, "purpose": "secret", "task_category": "unit_testing"}) == {
            "command": COMMAND
        }


@pytest.mark.parametrize(
    "policy",
    [
        {"version": 2, "mode": "command_only"},
        {"version": True, "mode": "command_only"},
        {"version": 1, "mode": "guess"},
        {"version": 1, "mode": "command_only", "purpose": "publish"},
    ],
)
def test_unknown_policy_is_rejected(context, policy):
    with pytest.raises(GateError):
        make_handoff(ORIGIN, context, REFERENCE, policy)


@pytest.mark.parametrize("field", ["privacy_policy", "scope", "schema_version", "extra"])
def test_child_cannot_change_policy_or_scope(context, field):
    trusted = envelope(context)
    child = copy.deepcopy(trusted)
    if field == "privacy_policy":
        child[field]["mode"] = "private_details"
    elif field == "scope":
        child[field]["bot_uuid"] = "another-bot"
    else:
        child[field] = 2
    with pytest.raises(GateError):
        final(context, trusted, {"command": COMMAND}, child=child)


@pytest.mark.parametrize("field", ["origin", "instance", "bot_uuid", "bot_id", "actor", "reference", "disabled"])
def test_wrong_service_bot_actor_or_key_reference_is_rejected(context, field):
    trusted = envelope(context)
    current = copy.deepcopy(context)
    changes = {}
    if field == "origin":
        changes["origin"] = "https://other.example.invalid"
    elif field == "instance":
        current["server"]["instance_id"] = "instance-b"
    elif field == "bot_uuid":
        current["bot"]["uuid"] = "bot-b"
    elif field == "bot_id":
        current["bot"]["id"] = 8
    elif field == "actor":
        current["actor"]["external_user_id"] = "user-b"
    elif field == "reference":
        changes["credential_ref"] = "keyring:another-bot"
    else:
        current["agent_access_enabled"] = False
    with pytest.raises(GateError):
        final(current, trusted, {"command": COMMAND}, **changes)


def test_raw_keys_and_invalid_commands_do_not_pass_gate(context):
    with pytest.raises(GateError):
        scope_from_authenticated_context(ORIGIN, context, "lbr_example-not-a-reference")
    for command in (None, "", "   ", "query\nlock node-b", "x" * 513):
        with pytest.raises(GateError):
            final(context, envelope(context), {"command": command})


def lease_arguments(context):
    start, expiry = "2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00"
    return {
        "origin": ORIGIN,
        "authenticated_context": context,
        "credential_ref": REFERENCE,
        "resource_id": "node-a:dev0",
        "saved": {"resource_id": "node-a:dev0", "actor": context["actor"], "start_time": start},
        "holder": {**context["actor"], "start_time": start, "expires_at": expiry},
        "now": "2026-01-01T10:30:00+00:00",
    }


def test_lease_evidence_uses_real_actor_and_start_not_agent_label(context):
    args = lease_arguments(context)
    trusted = envelope(context)
    assert lease_matches(trusted, **args)
    args["holder"]["agent_label"] = "a-different-or-missing-label"
    assert lease_matches(trusted, **args)
    args["saved"]["holding_id"] = "holding-a"
    assert not lease_matches(trusted, **args)
    args["holder"]["holding_id"] = "holding-a"
    assert lease_matches(trusted, **args)
    args["holder"]["holding_id"] = "holding-b"
    assert not lease_matches(trusted, **args)


@pytest.mark.parametrize("field", ["resource", "actor", "start", "expired"])
def test_lease_reuse_other_actor_or_expiry_is_not_our_task(context, field):
    args = lease_arguments(context)
    if field == "resource":
        args["resource_id"] = "node-b:dev0"
    elif field == "actor":
        args["holder"]["external_user_id"] = "user-b"
    elif field == "start":
        args["holder"]["start_time"] = "2026-01-01T10:01:00+00:00"
    else:
        args["now"] = "2026-01-01T11:00:00+00:00"
    assert not lease_matches(envelope(context), **args)


def test_feedback_uses_verified_matching_manifest_without_a_send_path(context):
    trusted = envelope(context)
    manifest = {**context["server"], "issues_url": "https://code.example.invalid/team/resources/issues"}
    assert feedback_url(trusted, manifest_origin=ORIGIN, verified_manifest=manifest) == manifest["issues_url"]
    for change in (
        {"instance_id": "other"},
        {"service_id": "other"},
        {"issues_url": "http://unverified.example.invalid/issues"},
    ):
        with pytest.raises(GateError):
            feedback_url(trusted, manifest_origin=ORIGIN, verified_manifest={**manifest, **change})
    with pytest.raises(GateError):
        feedback_url(trusted, manifest_origin="https://other.example.invalid", verified_manifest=manifest)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@code.example.invalid/issues",
        "https://code.example.invalid:bad/issues",
        "https://code.example.invalid:0/issues",
        "https://bad host.invalid/issues",
        "javascript:alert(1)",
    ],
)
def test_malformed_feedback_entries_are_rejected(context, url):
    with pytest.raises(GateError):
        feedback_url(
            envelope(context), manifest_origin=ORIGIN, verified_manifest={**context["server"], "issues_url": url}
        )
