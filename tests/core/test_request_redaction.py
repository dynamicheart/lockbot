import logging
from unittest.mock import Mock, patch

import requests
from lockbot.core.config import Config
from lockbot.core.request import _post_with_retry, post_webhook


def test_failed_response_does_not_log_webhook_or_response_body(caplog):
    secret = "CANARY_WEBHOOK_ACCESS_TOKEN"
    response = Mock(status_code=503, text=secret)
    with caplog.at_level(logging.DEBUG), patch("requests.post", return_value=response), patch("time.sleep"):
        assert _post_with_retry("https://example.test/?access_token=" + secret, {}, {})[0] == 503
    assert secret not in caplog.text
    assert "503" in caplog.text


def test_transport_exception_does_not_expose_url_in_log_or_result(caplog):
    secret = "CANARY_EXCEPTION_ACCESS_TOKEN"
    with (
        caplog.at_level(logging.DEBUG),
        patch("requests.post", side_effect=requests.ConnectionError("https://example.test/?access_token=" + secret)),
        patch("time.sleep"),
    ):
        result = _post_with_retry("https://example.test/?access_token=" + secret, {}, {})
    assert result == (None, "ConnectionError")
    assert secret not in caplog.text


def test_debug_logging_does_not_include_message_content(caplog):
    secret = "CANARY_PRIVATE_MESSAGE"
    message = {"message": {"header": {}, "body": [{"type": "TEXT", "content": secret}]}}
    with caplog.at_level(logging.DEBUG), patch("requests.post", return_value=Mock(status_code=200, text="ok")):
        post_webhook(message, Config({"WEBHOOK_URL": "https://example.test/"}))
    assert secret not in caplog.text
