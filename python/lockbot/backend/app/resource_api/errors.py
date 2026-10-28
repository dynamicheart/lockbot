"""Error contract for the agent-facing API.

Every failure carries a stable machine-readable ``code`` so an agent can branch on it
without string-matching prose. The codes an agent must distinguish, and why:

- ``command_not_allowed`` — the operation is never reachable through a credential
  (preemptive or administrative). Retrying is pointless.
- ``command_not_supported`` — this *bot type* has no such operation (``book`` on a DEVICE
  bot). Also pointless to retry, but it is a different mistake and gets a different code.
- ``aliases_disabled`` — the deployment turned the feature off. Distinguishable from
  ``command_not_allowed`` on purpose: the agent is permitted, the bot is not configured.
- ``bot_not_running`` / ``instance_retiring`` — transient; retry later.
- ``rate_limited`` / ``admission_full`` — back off, honour ``Retry-After``.
- ``may_have_executed`` — the operation was admitted and the outcome is unknown. Do NOT
  retry blindly; read ``/resources``.
"""

from __future__ import annotations

from fastapi import HTTPException, status


def api_error(
    http_status: int,
    code: str,
    message: str,
    *,
    retry_after: int | None = None,
) -> HTTPException:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message},
        headers=headers,
    )


def command_not_allowed(command: str) -> HTTPException:
    return api_error(
        status.HTTP_403_FORBIDDEN,
        "command_not_allowed",
        f"Command {command!r} is not reachable through an agent credential. "
        f"Preemptive and administrative operations are chat-only by design.",
    )


def command_not_supported(command: str, bot_type: str) -> HTTPException:
    return api_error(
        status.HTTP_400_BAD_REQUEST,
        "command_not_supported",
        f"A {bot_type} bot has no {command!r} operation. See the supported list in /resources.",
    )


def aliases_disabled() -> HTTPException:
    return api_error(
        status.HTTP_409_CONFLICT,
        "aliases_disabled",
        "This bot has ALLOW_USER_ALIAS turned off, so aliases cannot be set. "
        "You are permitted to call alias; the bot is not configured for it.",
    )


def bot_not_running(bot_id: int) -> HTTPException:
    return api_error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "bot_not_running",
        f"Bot {bot_id} is not running. Nothing will expire while it is down.",
        retry_after=30,
    )


def instance_retiring(bot_id: int) -> HTTPException:
    return api_error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "instance_retiring",
        f"Bot {bot_id} is being stopped, restarted or restored. No new work is accepted.",
        retry_after=10,
    )


def admission_full(bot_id: int) -> HTTPException:
    return api_error(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "admission_full",
        f"Bot {bot_id} already has the maximum number of operations outstanding.",
        retry_after=5,
    )


def credential_invalid() -> HTTPException:
    """The credential stopped being valid between authentication and execution.

    A restore, delete or revoke committed inside the admission window, so the actor resolved
    before admission is no longer authorized. Same opaque answer as the initial auth failure.
    """
    return api_error(
        status.HTTP_401_UNAUTHORIZED,
        "credential_invalid",
        "Credential is no longer valid for this bot. Re-enrol in chat if this persists.",
    )


def rate_limited(retry_after: int) -> HTTPException:
    """This credential exceeded its per-minute read or write budget. Back off and retry.

    Distinct from ``admission_full`` (a transient per-bot capacity signal): this is a
    sustained-rate budget scoped to the calling credential, not the bot.
    """
    return api_error(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "rate_limited",
        "This credential is sending requests faster than its budget allows. Honour Retry-After.",
        retry_after=retry_after,
    )


def identity_cap_reached(cap: int) -> HTTPException:
    """This identity already holds/books the maximum number of resources it may via the API.

    Not a rate signal: free a resource (unlock) before acquiring another. Waiting will not help.
    """
    return api_error(
        status.HTTP_409_CONFLICT,
        "identity_cap_reached",
        f"You already hold or have booked the maximum of {cap} resources via the API. "
        f"Release one before acquiring another.",
    )


def may_have_executed(bot_id: int) -> HTTPException:
    return api_error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "may_have_executed",
        f"The operation was admitted but its outcome is unknown. It may already have taken "
        f"effect. Read GET /api/bots/{bot_id}/resources to see what is actually true. "
        f"Do not blindly retry: a repeated lock extends duration.",
    )


def admission_http_error(exc: Exception, bot_id: int, *, legacy_not_running: bool = False) -> HTTPException:
    """Map a caught ``BotManager`` admission exception to the agent-facing HTTP error.

    Callers do ``except (BotNotRunning, InstanceRetiring, AdmissionFull, MayHaveExecuted) as
    exc: raise admission_http_error(exc, bot_id) from None`` so the four-way mapping lives in
    one place. ``legacy_not_running=True`` keeps the plain-string 503 the legacy service-key
    routes (``locked-users``/``opkick``) have always returned for a stopped bot.
    """
    # Imported lazily to avoid any import-order coupling between the error contract and the
    # manager module. The exception classes are siblings (all subclass RuntimeError), so the
    # isinstance order among them is irrelevant.
    from lockbot.backend.app.bots.manager import (
        AdmissionFull,
        BotNotRunning,
        InstanceRetiring,
        MayHaveExecuted,
    )

    if isinstance(exc, MayHaveExecuted):
        return may_have_executed(bot_id)
    if isinstance(exc, InstanceRetiring):
        return instance_retiring(bot_id)
    if isinstance(exc, AdmissionFull):
        return admission_full(bot_id)
    if isinstance(exc, BotNotRunning):
        if legacy_not_running:
            return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Bot not running")
        return bot_not_running(bot_id)
    raise exc  # not an admission exception; do not swallow it
