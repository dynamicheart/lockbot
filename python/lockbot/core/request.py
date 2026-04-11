"""HTTP webhook request utilities."""

import json
import logging
import time

import requests

from lockbot.core.config import Config

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_RETRY_DELAY = 1.0
_DEFAULT_HEADERS = {"Content-Type": "application/json"}


def post_webhook(msg, config=None):
    """Send a message via webhook, splitting long TEXT content into chunks.

    Args:
        msg: Message dict with structure {"message": {"header": {}, "body": []}}.
        config: Optional Config instance; uses global Config if None.

    Returns:
        list of (status_code, response_text) tuples.
    """
    MAX_LENGTH = 2000
    if config is not None:
        webhook_url = config.get_val("WEBHOOK_URL")
    else:
        webhook_url = Config.get("WEBHOOK_URL")

    # Extract the first TEXT body; everything after it goes to the last chunk only
    text_body = None
    trailing_bodies = []
    for body in msg["message"]["body"]:
        if body.get("type") == "TEXT" and text_body is None:
            text_body = body
        else:
            trailing_bodies.append(body)

    new_msgs = []
    if text_body:
        content = text_body["content"]
        # Split long content: prefer newline near end, otherwise hard-split at MAX_LENGTH
        while len(content) > MAX_LENGTH:
            split_index = content.rfind("\n", 0, MAX_LENGTH)
            if split_index == -1 or split_index < int(MAX_LENGTH * 0.8):
                split_index = MAX_LENGTH
            part = content[:split_index]
            new_msgs.append(
                {
                    "message": {
                        "header": msg["message"]["header"],
                        "body": [{"type": "TEXT", "content": part}],
                    }
                }
            )
            content = content[split_index:].lstrip()
        new_msgs.append(
            {
                "message": {
                    "header": msg["message"]["header"],
                    "body": [{"type": "TEXT", "content": content}] + trailing_bodies,
                }
            }
        )
    else:
        new_msgs.append(msg)

    responses = []
    for i, new_msg in enumerate(new_msgs):
        logger.info("Webhook payload [%d/%d]: %s", i + 1, len(new_msgs), json.dumps(new_msg, ensure_ascii=False))
        resp = _post_with_retry(webhook_url, new_msg, _DEFAULT_HEADERS)
        responses.append(resp)
    return responses


def _post_with_retry(url, payload, headers):
    """POST with simple retry on failure. Returns (status_code, response_text)."""
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=5)
            if 200 <= response.status_code < 300:
                return (response.status_code, response.text)
            logger.warning(
                "Webhook POST to %s returned %d (attempt %d/%d): %s",
                url,
                response.status_code,
                attempt + 1,
                _MAX_RETRIES + 1,
                response.text[:200],
            )
        except requests.exceptions.RequestException as e:
            last_exc = e
            logger.warning(
                "Webhook POST to %s failed (attempt %d/%d): %s",
                url,
                attempt + 1,
                _MAX_RETRIES + 1,
                e,
            )
        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_DELAY)

    if last_exc:
        logger.error("Webhook POST to %s failed after %d attempts: %s", url, _MAX_RETRIES + 1, last_exc)
        return (None, str(last_exc))
    # All attempts returned non-2xx — return the last response
    return (response.status_code, response.text)
