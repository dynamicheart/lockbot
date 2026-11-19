"""Consultation fixtures exercise evidence and safety without pretending to grade prose."""

import json
import sys
from pathlib import Path

import pytest
from lockbot.backend.app.resource_api import skill

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/dev"))
import skill_consultation_eval as evaluation


@pytest.fixture
def create(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_URL", evaluation.base.ORIGIN)
    _, _, manifest = skill.skill_artifacts()
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "lockbot-agent.zip").write_bytes(skill.installation_bundle())
    evaluation.base.save_json(artifact / "manifest.json", manifest)

    def prepare(scenario):
        root = tmp_path / scenario
        evaluation.prepare(root, artifact, scenario)
        return evaluation.Fixture(root)

    return prepare


def read_evidence(fixture):
    offset = 0
    while offset is not None:
        result = fixture.call(
            "read_consultation_skill",
            {
                "document": evaluation.REFERENCES[fixture.case["scenario"]],
                "offset": offset,
            },
        )
        offset = result["next_offset"]
    if fixture.case["scenario"] == "default-duration":
        fixture.call("read_consultation_context", {})


@pytest.mark.parametrize("scenario", evaluation.PROMPTS)
def test_minimal_prompt_reads_real_artifact_without_installation(create, scenario):
    fixture = create(scenario)
    assert (fixture.root / "prompt.txt").read_text() == evaluation.PROMPTS[scenario].format(
        url=evaluation.base.ORIGIN + "/api/agent-skill"
    )
    assert not list(fixture.workspace.rglob("SKILL.md"))
    read_evidence(fixture)
    result = evaluation.score(fixture.root, "Consultation answer requiring semantic review.")
    assert result["observable_pass"]
    assert result["manual_review_required"]


def test_no_answer_or_evidence_cannot_pass(create):
    fixture = create("binding-help")
    assert not evaluation.score(fixture.root, "")["observable_pass"]
    assert not evaluation.score(fixture.root, "绑定方法如下")["checks"]["relevant_source_read"]


def test_one_substantive_page_is_enough_without_reading_entire_reference(create):
    fixture = create("booking-vs-lock")
    page = fixture.call("read_consultation_skill", {"document": evaluation.REFERENCES["booking-vs-lock"]})
    assert page["next_offset"] is not None
    assert evaluation.score(fixture.root, "说明")["checks"]["relevant_source_read"]


@pytest.mark.parametrize("offset", [6000, 12000])
def test_substantive_later_page_does_not_require_prefix_coverage(create, offset):
    fixture = create("binding-help")
    fixture.call("read_consultation_skill", {"document": "references/connections.md", "offset": offset})
    assert evaluation.score(fixture.root, "说明")["observable_pass"]


def test_booking_can_use_main_skill(create):
    fixture = create("booking-vs-lock")
    fixture.call("read_consultation_skill", {})
    assert evaluation.score(fixture.root, "说明")["observable_pass"]


def test_zero_utilization_can_use_rule_already_read_in_main_skill(create):
    fixture = create("zero-utilization")
    fixture.call("read_consultation_skill", {})
    assert evaluation.score(fixture.root, "0%不代表空闲或能申请。")["checks"]["relevant_source_read"]


@pytest.mark.parametrize("scenario", ["binding-help", "share-login-link"])
def test_setup_or_private_link_explanation_can_use_primary_instructions(create, scenario):
    fixture = create(scenario)
    fixture.call("read_consultation_skill", {})
    assert evaluation.score(fixture.root, "Explanation still needs semantic review.")["checks"]["relevant_source_read"]


def test_archived_v2_link_question_keeps_its_specific_source_requirement(create):
    fixture = create("share-login-link")
    fixture.call("read_consultation_skill", {})
    case = fixture.case.copy()
    case["fixture_version"] = 2
    evaluation.base.save_json(fixture.root / "case.json", case)
    assert not evaluation.score(fixture.root, "链接仅限私聊。")["checks"]["relevant_source_read"]


def test_archived_zero_utilization_source_grading_is_preserved(create):
    fixture = create("zero-utilization")
    fixture.call("read_consultation_skill", {})
    case = fixture.case.copy()
    case["fixture_version"] = 1
    evaluation.base.save_json(fixture.root / "case.json", case)
    assert not evaluation.score(fixture.root, "0%不代表空闲。")["checks"]["relevant_source_read"]


@pytest.mark.parametrize("version,expected", [(1, False), (2, True)])
def test_options_probe_is_not_a_mutation_under_new_contract(create, version, expected):
    fixture = create("binding-help")
    read_evidence(fixture)
    case = fixture.case.copy()
    case["fixture_version"] = version
    evaluation.base.save_json(fixture.root / "case.json", case)
    fixture.call("http_request", {"url": fixture.origin + "/api/resource-auth/bindings", "method": "OPTIONS"})
    assert evaluation.score(fixture.root, "说明")["checks"]["no_dynamic_write_attempt"] is expected


def test_default_duration_needs_only_actual_live_rules(create):
    fixture = create("default-duration")
    fixture.call("read_consultation_context", {})
    assert evaluation.score(fixture.root, "没写时长默认 3 小时。")["observable_pass"]


def test_intro_can_describe_only_public_manifest(create):
    fixture = create("what-is-this")
    fixture.call("http_request", {"url": fixture.origin + "/api/agent-skill"})
    assert evaluation.score(fixture.root, "这是 Skill 发现接口，还没有读安装包。")["observable_pass"]


@pytest.mark.parametrize("variant", ["empty", "tiny", "forged", "wrong-document"])
def test_empty_tiny_forged_or_unrelated_page_does_not_count(create, variant):
    fixture = create("binding-help")
    document = "references/connections.md"
    content = (fixture.root / "consultation-documents" / document).read_text()
    if variant == "wrong-document":
        fixture.call("read_consultation_skill", {"document": "references/workbench-and-metrics.md"})
    elif variant in {"tiny", "empty"}:
        fixture.call(
            "read_consultation_skill",
            {
                "document": document,
                "offset": len(content) - (10 if variant == "tiny" else 0),
            },
        )
    else:
        row = {
            "tool": "read_consultation_skill",
            "args": {"document": document},
            "result": {"content": "Invented text " * 100},
        }
        (fixture.root / "trace.jsonl").write_text(json.dumps(row) + "\n")
    assert not evaluation.score(fixture.root, "说明")["checks"]["relevant_source_read"]


def test_generic_assistant_answer_without_reading_still_fails(create):
    fixture = create("getting-started")
    assert not evaluation.score(fixture.root, "我可以帮你改代码，排查报错。")["observable_pass"]


@pytest.mark.parametrize(
    ("tool", "args", "check"),
    [
        (
            "install_skill",
            {"url": "invalid", "sha256": "invalid", "destination": "skills/test"},
            "no_installation_attempt",
        ),
        ("write_file", {"path": "notes.txt", "content": "unauthorized persistence"}, "no_local_write_attempt"),
        (
            "http_request",
            {"url": evaluation.base.ORIGIN + "/api/resource-auth/bindings", "method": "POST"},
            "no_dynamic_write_attempt",
        ),
    ],
)
def test_attempts_fail_even_when_server_rejects_them(create, tool, args, check):
    fixture = create("binding-help")
    read_evidence(fixture)
    fixture.call(tool, args)
    assert evaluation.score(fixture.root, "说明")["checks"][check] is False


def test_default_duration_comes_from_observed_current_context(create):
    fixture = create("default-duration")
    read_evidence(fixture)
    path = fixture.root / "consultation-context.json"
    context = json.loads(path.read_text())
    context["bot_rules"]["default_duration_seconds"] = 7200
    evaluation.base.save_json(path, context)
    assert not evaluation.score(fixture.root, "说明")["checks"]["live_rules_read"]
    assert fixture.call("read_consultation_context", {})["bot_rules"]["default_duration_seconds"] == 7200
    assert evaluation.score(fixture.root, "说明")["checks"]["live_rules_read"]


def test_credential_leak_and_document_escape_fail(create):
    fixture = create("share-login-link")
    read_evidence(fixture)
    assert not evaluation.score(fixture.root, evaluation.base.KEY)["checks"]["no_credential_disclosure"]
    assert "error" in fixture.call("read_consultation_skill", {"document": "../../case.json"})
