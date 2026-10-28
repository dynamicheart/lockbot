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
