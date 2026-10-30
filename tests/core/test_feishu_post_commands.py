"""Only direct, unambiguous addressed posts can become resource commands.

Wire shapes follow Feishu's receive-message-content documentation:
https://open.feishu.cn/document/server-docs/im-v1/message-content-description/message_content
"""

import json

import pytest
from core.test_feishu_transport import event, identity
from lockbot.core.platforms.feishu_events import InvalidFeishuEvent, normalize_message


def post_event(rows, **post_fields):
    value = event()
    value["event"]["message"]["message_type"] = "post"
    value["event"]["message"]["content"] = json.dumps({"title": "", "content": rows, **post_fields})
    return value


AT = {"tag": "at", "user_id": "@_user_1", "user_name": ""}
CODE = {"tag": "code_block", "language": "BASH", "text": "bind ONE_TIME_TEST_PROOF"}


@pytest.mark.parametrize("shape", ["direct", "zh_cn", "en_us", "wrapped"])
def test_single_code_block_preserves_exact_binding_command(shape):
    value = post_event([[AT], [CODE]])
    if shape != "direct":
        content = json.loads(value["event"]["message"]["content"])
        content = {"en_us" if shape == "en_us" else "zh_cn": content}
        value["event"]["message"]["content"] = json.dumps({"post": content} if shape == "wrapped" else content)
    result = normalize_message(value, identity())
    assert result == {
        "user_id": "ou_alice",
        "group_id": "oc_demo",
        "command": "bind ONE_TIME_TEST_PROOF",
        "message_id": "om_1",
    }


@pytest.mark.parametrize("reference", ["@_user_1", "ou_bot"])
def test_structured_mention_with_one_code_block_is_supported(reference):
    value = post_event([[dict(AT, user_id=reference)], [CODE]])
    assert normalize_message(value, identity())["command"] == CODE["text"]


def test_matching_content_v2_is_not_mistaken_for_multiple_commands():
    rows = [[AT], [CODE]]
    assert normalize_message(post_event(rows, content_v2=rows), identity())["command"] == CODE["text"]


@pytest.mark.parametrize(
    "rows,fields",
    [
        ([[AT], [CODE], [CODE]], {}),
        ([[AT], [dict(CODE, text=" ")]], {}),
        ([[AT], [{"tag": "text", "text": "lock node_a 5m"}]], {}),
        ([[AT], [CODE]], {"title": "An example, not an instruction"}),
        ([[AT, {"tag": "text", "text": "Example:"}], [CODE]], {}),
        ([[AT], [dict(CODE, text="bind ONE_TIME_TEST_PROOF\nlock node_a 5m")]], {}),
        ([[AT], [dict(CODE, text="bind ONE_TIME_TEST_PROOF; lock node_a 5m")]], {}),
        ([[AT], [dict(CODE, text="bind ONE_TIME_TEST_PROOF\u2028lock node_a 5m")]], {}),
        ([[AT], [{"tag": "quote", "content": [[CODE]]}]], {}),
        ([[AT], [{"tag": "md", "text": "```\nbind ONE_TIME_TEST_PROOF\n```"}]], {}),
        ([[AT], [{"tag": "a", "text": CODE["text"], "href": "https://example.invalid"}]], {}),
        ([[AT], [{"tag": "text", "text": CODE["text"], "style": ["lineThrough"]}]], {}),
        (
            [[AT], [{"tag": "text", "text": CODE["text"]}]],
            {"content_v2": [[AT], [{"tag": "md", "text": "> bind ONE_TIME_TEST_PROOF"}]]},
        ),
    ],
)
def test_ambiguous_or_quoted_post_becomes_safe_format_feedback(rows, fields):
    result = normalize_message(post_event(rows, **fields), identity())
    assert result["command_format_error"] is True
    assert result["command"] == "__unsupported_rich_text__"
    assert "ONE_TIME_TEST_PROOF" not in str(result)


@pytest.mark.parametrize("change", ["no_mention", "another_bot", "only_literal_at", "quoted_at", "two_locales"])
def test_post_does_not_infer_addressing_from_text_or_nested_content(change):
    value = post_event([[AT], [CODE]])
    message = value["event"]["message"]
    if change == "no_mention":
        message["mentions"] = []
    elif change == "another_bot":
        message["mentions"][0]["id"]["open_id"] = "ou_other"
    elif change == "only_literal_at":
        message["content"] = json.dumps({"content": [[{"tag": "text", "text": "@_user_1 " + CODE["text"]}]]})
    elif change == "quoted_at":
        message["content"] = json.dumps({"content": [[{"tag": "quote", "content": [[AT, CODE]]}]]})
    else:
        message["content"] = json.dumps({"zh_cn": {"content": [[AT, CODE]]}, "en_us": {"content": [[AT, CODE]]}})
    assert normalize_message(value, identity()) is None


def test_post_identity_and_freshness_checks_still_apply():
    value = post_event([[AT], [CODE]])
    value["event"]["message"]["create_time"] = "1"
    with pytest.raises(InvalidFeishuEvent):
        normalize_message(value, identity())
    value = post_event([[AT], [CODE]])
    value["header"]["tenant_key"] = "other"
    with pytest.raises(InvalidFeishuEvent):
        normalize_message(value, identity())
