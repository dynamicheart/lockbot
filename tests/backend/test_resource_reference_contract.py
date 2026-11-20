"""References keep exact resources separate without borrowing names or authority."""

import copy
import json
import socket

import pytest

from backend.test_resource_agent_helper import helper


def record(**overrides):
    return {
        "approved_origin": "http://example.test:8155",
        "instance_id": "service-instance",
        "bot_uuid": "bot-uuid",
        "actor": {"platform": "infoflow", "external_user_id": "alice"},
        "key_file": "credential",
        **overrides,
    }


@pytest.mark.parametrize("node", ["node7", "node07", "node/7", "node%2F7", "node 7", "节点7", "dev0"])
def test_reference_preserves_exact_identifier(node):
    value = helper.resource_reference(record(), node)
    digest, observed = helper.parse_resource_reference(value)
    assert observed == node and helper.resource_scope_digest(record()).startswith(digest)


def test_reference_uses_pinned_identity_instead_of_names_paths_or_key_material():
    original = record()
    reference = helper.resource_reference(original, "node07")
    changed = record(name="robot034", connection="/elsewhere/connection.json", key_file="rotated-key")
    assert helper.resource_reference(changed, "node07") == reference
    for changed in (
        record(instance_id="replacement-instance"),
        record(bot_uuid="replacement-bot-same-integer-id", id=7),
        record(actor={"platform": "infoflow", "external_user_id": "bob"}),
        record(actor={"platform": "feishu", "external_user_id": "alice"}),
        record(approved_origin="http://other.example.test:8155"),
    ):
        assert helper.resource_reference(changed, "node07") != reference
    assert original == record()


@pytest.mark.parametrize(
    "value",
    [
        None,
        "node07",
        "s-short/node07",
        "s-" + "a" * 12 + "/node%30%37",
        "s-" + "a" * 12 + "/node%2f7",
        "s-" + "a" * 12 + "/%FF",
        "s-" + "a" * 12 + "/",
        "s-" + "a" * 12 + "/%0A",
    ],
)
def test_noncanonical_and_unusable_references_are_rejected(value):
    with pytest.raises(helper.ClientError, match="invalid_resource_reference"):
        helper.parse_resource_reference(value)


def save(tmp_path, name, value):
    directory = tmp_path / name
    directory.mkdir(mode=0o700)
    path = directory / "connection.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def test_local_resolution_does_not_open_keys_or_contact_network(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    value = record(key_file="does-not-exist")
    expected = save(tmp_path, "first", value)
    save(tmp_path, "other", record(bot_uuid="other-bot"))

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("Local reference routing must not contact any service")

    monkeypatch.setattr(socket, "create_connection", network_forbidden)
    result = helper.resolve_resource_reference(helper.resource_reference(value, "node07"), tmp_path)
    assert result["node"] == "node07" and result["online_verified"] is False
    assert [row["connection"] for row in result["connections"]] == [str(expected)]
    assert not expected.with_name("does-not-exist").exists()


def test_equivalent_records_preserve_both_credential_candidates_without_merging_capabilities(tmp_path):
    tmp_path.chmod(0o700)
    first = record(capabilities=["read"], key_file="one-key")
    second = record(capabilities=["read", "command"], key_file="another-key")
    paths = [save(tmp_path, "one", first), save(tmp_path, "two", second)]
    before = {str(p): p.read_bytes() for p in paths}
    result = helper.resolve_resource_reference(helper.resource_reference(first, "node07"), tmp_path)
    assert [row["connection"] for row in result["connections"]] == sorted(map(str, paths))
    assert "capabilities" not in result and result["online_verified"] is False
    assert before == {str(p): p.read_bytes() for p in paths}


def test_prefix_collision_does_not_choose_the_first_record(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    save(tmp_path, "one", record())
    save(tmp_path, "two", record(bot_uuid="different-bot"))
    monkeypatch.setattr(helper, "resource_scope_digest", lambda _record: "a" * 64)
    with pytest.raises(helper.ClientError, match="resource_reference_scope_ambiguous"):
        helper.resolve_resource_reference("s-" + "a" * 12 + "/node07", tmp_path)


def test_old_reference_cannot_follow_integer_id_reuse_or_another_principal(tmp_path):
    tmp_path.chmod(0o700)
    original = record(id=7)
    old = helper.resource_reference(original, "node07")
    save(tmp_path, "replacement", record(id=7, bot_uuid="new-bot"))
    save(tmp_path, "another-user", record(actor={"platform": "infoflow", "external_user_id": "bob"}))
    with pytest.raises(helper.ClientError, match="resource_reference_scope_unavailable"):
        helper.resolve_resource_reference(old, tmp_path)


def test_incomplete_private_catalog_is_reported_without_an_identity_guess(tmp_path):
    tmp_path.chmod(0o700)
    value = record()
    save(tmp_path, "good", value)
    malformed = save(tmp_path, "broken", copy.deepcopy(value))
    malformed.write_text("not valid private metadata")
    with pytest.raises(helper.ClientError, match="resource_reference_catalog_incomplete"):
        helper.resolve_resource_reference(helper.resource_reference(value, "node07"), tmp_path)
