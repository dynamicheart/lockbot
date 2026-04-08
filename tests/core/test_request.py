from unittest.mock import patch

import pytest
from lockbot.core.request import post_webhook


@pytest.fixture
def sample_msg_long_text():
    """Return a message dict with 2500 chars of plain text (no newlines)."""
    return {
        "message": {
            "header": {},
            "body": [{"type": "TEXT", "content": "A" * 2500}, {"atuserids": ["xx"], "type": "AT"}],
        }
    }


@pytest.fixture
def sample_msg_with_newlines():
    """Return a message with 1900 'A's, a newline, then 500 'B's (total 2401 chars)."""
    content = "A" * 1900 + "\n" + "B" * 500
    return {
        "message": {"header": {}, "body": [{"type": "TEXT", "content": content}, {"atuserids": ["xx"], "type": "AT"}]}
    }


@patch("requests.post")
def test_post_webhook_long_text(mock_post, sample_msg_long_text):
    """Test that long text (2500 chars) is split into two messages."""
    mock_post.return_value.status_code = 200
    mock_post.return_value.text = "OK"

    responses = post_webhook(sample_msg_long_text)
    assert len(responses) == 2
    for status, _ in responses:
        assert status == 200

    sent_msgs = [call.kwargs["json"]["message"] for call in mock_post.call_args_list]
    first_text = sent_msgs[0]["body"][0]["content"]
    second_text = sent_msgs[1]["body"][0]["content"]
    assert len(first_text) == 2000
    assert len(second_text) == 2500 - 2000


@patch("requests.post")
def test_post_webhook_with_newlines(mock_post, sample_msg_with_newlines):
    """Test webhook splits message at newline boundary when possible."""
    mock_post.return_value.status_code = 200
    mock_post.return_value.text = "OK"

    responses = post_webhook(sample_msg_with_newlines)
    assert len(responses) == 2
    for status, _ in responses:
        assert status == 200

    sent_msgs = [call.kwargs["json"]["message"] for call in mock_post.call_args_list]
    first_content = sent_msgs[0]["body"][0]["content"]
    second_content = sent_msgs[1]["body"][0]["content"]

    # First part: 1900 'A's (before newline)
    assert first_content == "A" * 1900
    # Second part: 500 'B's
    assert second_content == "B" * 500
