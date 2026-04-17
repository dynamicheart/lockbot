"""
In-process webhook handler.

Handles IM callbacks for bots running in shared-port (inprocess) mode.
Uses per-bot adapter (bot.adapter) so multiple bots and multiple IM platforms
can coexist in a single process.

Platform dispatch is owned entirely by each adapter via handle_webhook().
"""


def handle_webhook(
    bot,
    raw_form: dict,
    raw_args: dict,
    raw_body: bytes | None,
    raw_headers: dict | None = None,
) -> tuple[str, int, dict]:
    """
    Process an incoming IM webhook callback for a specific bot instance.

    Delegates to bot.adapter.handle_webhook() which owns the full request
    lifecycle for the platform (challenge handshake, signature verification,
    decryption, command execution).

    Args:
        bot: Bot instance with a .adapter attribute.
        raw_form: Parsed form data (application/x-www-form-urlencoded).
        raw_args: Query string parameters.
        raw_body: Raw request body bytes.
        raw_headers: HTTP request headers.

    Returns:
        Tuple of (response_text, status_code, meta).
        meta may contain: user_id, command, group_id, event.
    """
    headers = {k.lower(): v for k, v in (raw_headers or {}).items()}
    body = raw_body or b""
    return bot.adapter.handle_webhook(bot, raw_form, raw_args, body, headers)
