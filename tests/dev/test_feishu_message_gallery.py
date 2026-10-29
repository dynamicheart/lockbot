"""A rendering diagnostic must preserve links/mentions without interpreting plain text."""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "feishu_message_gallery", Path(__file__).parents[2] / "scripts/dev/feishu_message_gallery.py"
)
gallery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gallery)


def test_structured_links_mentions_and_blank_lines_are_preserved():
    message = gallery.CaptureAdapter().build_reply(
        ["✅ 标题\n\n正文\n\n", ("文档：", "https://example.test/docs"), "\n"], ["self"], group_id="1"
    )
    post = gallery.render_post(message, identities={"self": "ou_test"})["zh_cn"]
    rows = post["content"]
    assert rows[1] == [{"tag": "text", "text": " "}]
    assert rows[4][1] == {"tag": "a", "text": "https://example.test/docs", "href": "https://example.test/docs"}
    assert rows[-1] == [{"tag": "at", "user_id": "ou_test"}]


def test_literal_text_cannot_inject_mentions_and_fixture_ids_are_not_guessed():
    message = gallery.CaptureAdapter().build_reply('<at user_id="all">text</at> **plain**', ["alice"], group_id="1")
    nodes = gallery.render_post(message)["zh_cn"]["content"][0]
    assert all(node["tag"] == "text" for node in nodes)
    assert '<at user_id="all">' in nodes[0]["text"]
    message["message"]["body"].append({"type": "AT", "atall": True})
    assert gallery.render_post(message)["zh_cn"]["content"][0][-1] == {"tag": "at", "user_id": "all"}
    message["message"]["body"].append({"type": "LINK", "href": "javascript:alert(1)"})
    with pytest.raises(ValueError):
        gallery.render_post(message)


def test_catalog_uses_all_four_real_bot_renderers_on_disposable_state():
    cases = gallery.gallery()
    names = {case["name"] for case in cases}
    for kind in ("node", "device", "queue", "queue_device"):
        assert {f"{kind}-help", f"{kind}-lock", f"{kind}-query", f"{kind}-release"} <= names
    assert {"queue-book", "queue_device-book", "links", "emoji", "mention-all"} <= names
    locked = next(case for case in cases if case["name"] == "device-lock")
    assert "资源申请成功" in str(locked["infoflow"])
    for case in cases:
        assert gallery.render_post(case["infoflow"])["zh_cn"]["content"]


@pytest.fixture
def cli(tmp_path, monkeypatch):
    import json
    import sys

    target = tmp_path / "private-target.json"
    target.write_text(json.dumps({"chat_id": "oc_fixture", "tenant_access_token": "secret-fixture-token"}))
    report = tmp_path / "report.json"
    message = gallery.CaptureAdapter().build_reply("✅ fixture", [], group_id="1")
    monkeypatch.setattr(gallery, "gallery", lambda: [{"name": name, "infoflow": message} for name in ("a", "b")])
    monkeypatch.setattr(gallery.time, "sleep", lambda _: None)

    def run(*options):
        monkeypatch.setattr(sys, "argv", ["gallery", "--report", str(report), *options])
        gallery.main()

    return run, target, report


def test_dry_run_never_calls_transport_even_with_target_file(cli, monkeypatch, capsys):
    import json

    run, target, report = cli
    monkeypatch.setattr(gallery, "api", lambda *args: pytest.fail("dry-run attempted delivery"))
    run("--target-file", str(target))
    result = json.loads(report.read_text())
    assert all(case["status"] == "dry_run" for case in result["cases"])
    assert "secret-fixture-token" not in report.read_text() + capsys.readouterr().out


def test_send_requires_explicit_selected_target(cli, monkeypatch):
    run, _, _ = cli
    monkeypatch.setattr(gallery, "api", lambda *args: pytest.fail("missing-target attempted delivery"))
    with pytest.raises(SystemExit):
        run("--send")


def test_uncertain_delivery_stops_without_retry_or_later_message(cli, monkeypatch, capsys):
    import json

    run, target, report = cli
    sent = []

    def uncertain(*args):
        sent.append(args)
        raise gallery.requests.ConnectionError("secret-fixture-token transport detail")

    monkeypatch.setattr(gallery, "api", uncertain)
    with pytest.raises(SystemExit, match="no automatic retries"):
        run("--target-file", str(target), "--send")
    assert len(sent) == 1
    assert json.loads(report.read_text())["cases"][0]["status"] == "failed_or_uncertain"
    assert "secret-fixture-token" not in report.read_text() + str(capsys.readouterr())


def test_resume_keeps_target_scope_and_skips_accepted_cases(cli, monkeypatch):
    import json

    run, target, report = cli
    sent = []

    def accepted(*args):
        sent.append(args)
        return {"data": {"message_id": "om_fixture"}}

    monkeypatch.setattr(gallery, "api", accepted)
    run("--target-file", str(target), "--send")
    assert len(sent) == 2
    run("--target-file", str(target), "--send", "--resume")
    assert len(sent) == 2
    config = json.loads(target.read_text())
    config["chat_id"] = "oc_another_fixture"
    target.write_text(json.dumps(config))
    with pytest.raises(SystemExit):
        run("--target-file", str(target), "--send", "--resume")
    assert len(sent) == 2
    assert len(json.loads(report.read_text())["cases"]) == 2


def test_gallery_public_operation_example_omits_private_fields_and_real_service_addresses():
    cases = gallery.gallery()
    content = str(cases)
    assert "10.162.36.62" not in content
    operation = next(case for case in cases if case["name"] == "agent-operation")
    assert "任务分类：模型性能测试" in str(operation)
    assert "Agent：" not in str(operation) and "备注：" not in str(operation)


@pytest.mark.parametrize("status,body", [(500, {"code": 0}), (400, {"code": "secret-fixture-token"}), (200, [])])
def test_transport_rejects_non_success_or_malformed_acknowledgements_without_echoing_values(monkeypatch, status, body):
    from types import SimpleNamespace

    monkeypatch.setattr(
        gallery._session, "post", lambda *args, **kwargs: SimpleNamespace(status_code=status, json=lambda: body)
    )
    with pytest.raises(RuntimeError) as exc:
        gallery.api("secret-fixture-token", "im/v1/messages", {})
    assert "secret-fixture-token" not in str(exc.value)
