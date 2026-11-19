"""Existing user-catalog access is distinct from a loose seed key."""

import pytest
from dev.test_skill_cli_eval import install, run
from dev.test_skill_cli_v20 import versioned


@pytest.mark.parametrize("version", [30, 31])
def test_local_state_does_not_hide_an_existing_connection(tmp_path, monkeypatch, version):
    _, fixture = versioned(tmp_path, monkeypatch, "install-with-key", version)
    install(fixture)
    state = run(fixture, "local-state")["result"]
    assert state["seed_key_file_present"] is False
    if version == 31:
        assert state["saved_connection_present"] is True
        assert state["next_command"] == ["connections"]
        result = run(fixture, "connections")["result"]
        assert result["connections"]
        resources = run(fixture, "resources-all")["result"]
        assert resources["complete"]
        assert resources["groups"]
    else:
        assert "saved_connection_present" not in state
        assert "next_command" not in state


def test_known_zero_http_correction_is_separate_from_a_hardware_probe(tmp_path, monkeypatch):
    _, fixture = versioned(tmp_path, monkeypatch, "intent-model-p800-unknown-node", 32)
    assert (
        run(fixture, "command", operation="lock", model="P800", whole_node=True, duration="2h")["error"]
        == "invalid_cli_arguments"
    )
    run(fixture, "resources-all", model="P800")
    from dev.test_skill_cli_eval import evaluation

    final = "整机节点型号未知，不能确认是P800，未提交申请。"
    grade = evaluation.score(fixture.root, final)
    assert grade["observable_pass"], grade
    assert grade["cli_command_metrics"]["pre_http_argument_corrections"] == 1
    assert grade["cli_command_metrics"]["resource_posts"] == 0
    fixture.case["fixture_version"] = 31
    evaluation.base.save_json(fixture.root / "case.json", fixture.case)
    assert not evaluation.score(fixture.root, final)["checks"]["zero_write_attempts"]
