"""Identity-binding and credential service.

This module owns every SQL statement for resource auth, so the chat seam in ``core/`` can
stay database-free. It sees the IM platform only as an opaque ``(platform,
external_user_id)`` pair.

Trust rule, enforced structurally: the acting identity is only ever read from arguments the
webhook derived from a signature-verified, decrypted callback. No HTTP request body can
reach ``platform``, ``external_user_id`` or ``bound_bot_id``.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.orm import Session

from lockbot.backend.app.config import (
    RESOURCE_AUTH_BIND_FAILURE_WINDOW_SECONDS,
    RESOURCE_AUTH_BIND_WINDOW_SECONDS,
    RESOURCE_AUTH_CREDENTIAL_TTL_SECONDS,
    RESOURCE_AUTH_MAX_BIND_FAILURES,
    RESOURCE_AUTH_MAX_PENDING_PER_SOURCE,
    RESOURCE_AUTH_MAX_PENDING_TOTAL,
)
from lockbot.backend.app.resource_auth import credentials
from lockbot.backend.app.resource_auth.models import ResourceBinding
from lockbot.backend.app.resource_auth.pending import PendingCollision, pending_store
from lockbot.core.i18n import t

logger = logging.getLogger(__name__)

#: Statuses reported by `GET /api/resource-auth/bindings/{id}`.
STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REVOKED = "revoked"
STATUS_EXPIRED = "expired"

#: Attempts to find an unused binding id and code before giving up. A collision needs a
#: birthday coincidence among outstanding pending codes, so one retry is already generous.
_MAX_ISSUE_ATTEMPTS = 8


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive_utc(moment: datetime) -> datetime:
    """Drop the tzinfo for storage, since the existing columns are naive DateTime."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


# ── Chat bind attempt throttling ──────────────────────────────────────────────
# The HTTP creation limit does not bound chat attempts, so failed `bind` attempts are
# throttled separately, per (platform, external_user_id, bot).

_failures_lock = threading.Lock()
_bind_failures: dict[tuple[str, str, int], list[datetime]] = {}


def _record_bind_failure(key: tuple[str, str, int], now: datetime) -> None:
    with _failures_lock:
        window_start = now - timedelta(seconds=RESOURCE_AUTH_BIND_FAILURE_WINDOW_SECONDS)
        attempts = [a for a in _bind_failures.get(key, []) if a > window_start]
        attempts.append(now)
        _bind_failures[key] = attempts


def _is_bind_throttled(key: tuple[str, str, int], now: datetime) -> bool:
    with _failures_lock:
        window_start = now - timedelta(seconds=RESOURCE_AUTH_BIND_FAILURE_WINDOW_SECONDS)
        attempts = [a for a in _bind_failures.get(key, []) if a > window_start]
        if attempts:
            _bind_failures[key] = attempts
        else:
            _bind_failures.pop(key, None)
        return len(attempts) >= RESOURCE_AUTH_MAX_BIND_FAILURES


def reset_bind_throttle() -> None:
    """Clear throttling state. Test helper."""
    with _failures_lock:
        _bind_failures.clear()


# ── Issuance (unauthenticated, no durable write) ──────────────────────────────


def create_binding(*, source: str = "", label: str | None = None) -> dict:
    """Issue both keys in one call and record only an in-memory pending entry.

    Returns a dict with ``binding_id``, ``credential`` (inert until chat confirmation),
    ``bind_code`` and ``bind_expires_at``. The credential plaintext is returned here and
    nowhere else, ever.
    """
    # Regenerate on the astronomically rare id/code collision rather than overwrite a live
    # pending entry. A handful of attempts is far more than the birthday maths ever needs.
    for _ in range(_MAX_ISSUE_ATTEMPTS):
        binding_id = credentials.generate_public_id()
        credential = credentials.generate_credential()
        bind_code = credentials.generate_bind_code()
        try:
            record = pending_store.create(
                binding_id=binding_id,
                code_digest=credentials.digest_bind_code(bind_code),
                credential_digest=credentials.digest_credential(credential),
                source=source,
                label=(label or None),
                window_seconds=RESOURCE_AUTH_BIND_WINDOW_SECONDS,
                max_total=RESOURCE_AUTH_MAX_PENDING_TOTAL,
                max_per_source=RESOURCE_AUTH_MAX_PENDING_PER_SOURCE,
            )
            break
        except PendingCollision:
            continue
    else:
        raise RuntimeError("could not issue a unique pending binding")
    return {
        "binding_id": binding_id,
        "credential": credential,
        "bind_code": bind_code,
        "bind_expires_at": record.bind_expires_at,
    }


def binding_status(db: Session, binding_id: str) -> str:
    """Resolve a binding's status across the pending store and the durable table.

    Precedence matters: the durable table wins, and an id that is in neither store is
    ``expired`` rather than ``pending``, so a closed window can never be reported as still
    open.
    """
    row = db.query(ResourceBinding).filter(ResourceBinding.public_id == binding_id).one_or_none()
    if row is not None:
        return STATUS_REVOKED if row.revoked_at is not None else STATUS_CONFIRMED
    if pending_store.get(binding_id) is not None:
        return STATUS_PENDING
    return STATUS_EXPIRED


# ── Authentication ────────────────────────────────────────────────────────────


def authenticate(db: Session, *, bot_id: int, bot_uuid: str | None, token: str) -> ResourceBinding | None:
    """Return the binding a credential authenticates as, or None.

    The predicate requires all of: digest match, a confirmed durable row, not revoked,
    bot-scope match, the bot's uuid still matching the snapshot taken at confirmation (so a
    reused row id cannot transfer the credential), and — only when
    ``credential_expires_at`` is set — that it is still in the future.
    """
    if not credentials.looks_like_credential(token):
        return None

    from lockbot.backend.app.bots.models import Bot

    bot = db.get(Bot, bot_id)
    if bot is None or bot.is_deleted:
        return None
    digest = credentials.digest_credential(token)
    candidates = db.query(ResourceBinding).filter(ResourceBinding.credential_digest == digest).all()

    now = _utcnow()
    for row in candidates:
        if not credentials.credential_matches(row.credential_digest, token):
            continue
        if row.bound_identity_revision != bot.identity_revision:
            continue
        if row.revoked_at is not None:
            continue
        if row.bound_bot_id != bot_id:
            continue
        if bot_uuid and row.bound_bot_uuid != bot_uuid:
            continue
        if row.credential_expires_at is not None and row.credential_expires_at <= _naive_utc(now):
            continue
        return row
    return None


def touch_last_used(db: Session, row: ResourceBinding) -> None:
    """Record that a credential was used, for the chat listing and load attribution."""
    row.last_used_at = _naive_utc(_utcnow())


# ── Revocation ────────────────────────────────────────────────────────────────


def revoke_bindings_for_bot(db: Session, bot_id: int) -> int:
    """Revoke every durable credential scoped to a bot. Returns how many were revoked."""
    now = _naive_utc(_utcnow())
    rows = (
        db.query(ResourceBinding)
        .filter(ResourceBinding.bound_bot_id == bot_id, ResourceBinding.revoked_at.is_(None))
        .all()
    )
    for row in rows:
        row.revoked_at = now
    return len(rows)


def _credential_expiry(now: datetime) -> datetime | None:
    """Return the configured expiry, or None when credentials do not expire (the default)."""
    if RESOURCE_AUTH_CREDENTIAL_TTL_SECONDS <= 0:
        return None
    return _naive_utc(now + timedelta(seconds=RESOURCE_AUTH_CREDENTIAL_TTL_SECONDS))


# ── Chat-side enrollment and revocation ───────────────────────────────────────
# These are reachable only from chat, never over HTTP, so a user can always revoke without
# a platform login and an API caller can never enroll itself.


def _bot_language(bot) -> str:
    import contextlib
    import json

    lang = "zh"
    if bot is not None:
        with contextlib.suppress(Exception):
            lang = json.loads(bot.config_overrides or "{}").get("LANGUAGE", "zh")
    return lang


def handle_chat_binding(
    *, platform: str, external_user_id: str, bot_id, action: str, argument: str, expected_revision=None
) -> str:
    from lockbot.backend.app.resource_auth.authority import authority_guard

    with authority_guard(bot_id, platform=platform, external_user_id=external_user_id):
        return _handle_chat_binding(
            platform=platform,
            external_user_id=external_user_id,
            bot_id=bot_id,
            action=action,
            argument=argument,
            expected_revision=expected_revision,
        )


def _handle_chat_binding(
    *, platform: str, external_user_id: str, bot_id, action: str, argument: str, expected_revision=None
) -> str:
    """The hook injected into ``core/handler.py`` for `bind` / `unbind` / `bindings`.

    Every identity value here arrives from the verified, decrypted callback. Opens and
    closes its own session because it runs on the webhook worker, not in a request scope.
    """
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.database import SessionLocal

    db = SessionLocal()
    reserved_token = None
    try:
        bot = db.get(Bot, bot_id) if bot_id else None
        if bot is not None and bot.is_deleted:
            bot = None
        lang = _bot_language(bot)
        if bot is None:
            return t("binding.unavailable", lang=lang)
        if bot is not None and expected_revision is not None and bot.identity_revision != expected_revision:
            return t("binding.unavailable", lang=lang)

        if action == "bind":
            reply, reserved_token = _chat_bind(db, platform, external_user_id, bot, lang, argument)
        elif action == "bindings":
            reply = _chat_list(db, platform, external_user_id, bot, lang)
        elif action == "unbind":
            reply = _chat_unbind(db, platform, external_user_id, bot, lang, argument)
        else:
            reply = t("binding.failed", lang=lang)
        db.commit()
        # Confirm-then-persist: the reserved pending record is only dropped for good once the
        # durable row has committed. If the commit above had failed we would restore it below,
        # so the single-use code is never consumed by a write that did not land.
        if reserved_token is not None:
            pending_store.finalize(reserved_token)
        return reply
    except Exception:
        db.rollback()
        if reserved_token is not None:
            pending_store.restore(reserved_token)
        raise
    finally:
        db.close()


def _audit_binding_change(db, platform, external_user_id, bot, action, rows):
    """Record real authorization changes in the same transaction, without secrets."""
    if not rows:
        return
    from lockbot.backend.app.bot_audit.service import write_bot_audit

    write_bot_audit(
        db,
        SimpleNamespace(id=None, username=f"{platform}:{external_user_id}"[:64], role="agent"),
        f"resource_auth.{action}",
        target_type="bot",
        target_id=bot.id,
        target_name=bot.name,
        detail={"source": "chat", "platform": platform, "external_user_id": external_user_id},
    )


def _with_agent_usage_hint(db, bot, lang, reply):
    from urllib.parse import urlsplit

    from lockbot.backend.app.settings.router import get_all_settings

    if not bot.resource_auth_enabled:
        return reply
    base = (get_all_settings(db).get("platform_url") or "").strip().split("#", 1)[0].rstrip("/")
    try:
        parsed = urlsplit(base)
    except ValueError:
        return reply
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return reply
    return reply + "\n\n" + t("binding.agent_usage_hint", lang=lang, url=base + "/api/agent-skill/SKILL.md")


def _chat_bind(db: Session, platform: str, external_user_id: str, bot, lang: str, argument: str):
    """Confirm a bind code. Returns ``(reply, reservation_token_or_None)``.

    The pending record is *reserved* (hidden from other claimants but not yet destroyed)
    before the durable insert. The caller finalizes the returned token after the transaction
    commits, or restores it if the commit fails, so a failed durable write never strands the
    single-use code. ``None`` is returned as the token whenever no reservation was taken.
    """
    if bot is None:
        return t("binding.unavailable", lang=lang), None
    if not bot.resource_auth_enabled:
        return t("binding.enrollment_disabled", lang=lang), None

    code = credentials.normalize_bind_code(argument)
    if not code:
        return _with_agent_usage_hint(db, bot, lang, t("binding.usage_bind", lang=lang)), None

    now = _utcnow()
    throttle_key = (platform, external_user_id, bot.id)
    if _is_bind_throttled(throttle_key, now):
        return t("binding.throttled", lang=lang), None

    reservation = pending_store.reserve(credentials.digest_bind_code(code), now=now)
    if reservation is None:
        _record_bind_failure(throttle_key, now)
        return _with_agent_usage_hint(db, bot, lang, t("binding.invalid_or_expired", lang=lang)), None
    record, token = reservation
    if bot.binding_accept_after is not None and _naive_utc(record.created_at) <= bot.binding_accept_after:
        pending_store.restore(token)
        return _with_agent_usage_hint(db, bot, lang, t("binding.invalid_or_expired", lang=lang)), None

    try:
        previous = _own_bindings(db, platform, external_user_id, bot.id)
        if any(_binding_is_current(row, bot, now) for row in previous):
            # Never rotate an existing shared credential as a side effect of another
            # Agent's enrollment. Consume this code; its credential remains inert.
            return t("binding.already_bound", lang=lang, bot_id=bot.id), token
        for old in previous:
            old.revoked_at = _naive_utc(now)
        db.flush()
        row = ResourceBinding(
            public_id=record.binding_id,
            platform=platform,
            external_user_id=external_user_id,
            bound_bot_id=bot.id,
            bound_bot_uuid=bot.uuid or "",
            bound_identity_revision=bot.identity_revision,
            credential_digest=record.credential_digest,
            label=record.label,
            issued_at=_naive_utc(now),
            credential_expires_at=_credential_expiry(now),
        )
        db.add(row)
        db.flush()
        _audit_binding_change(db, platform, external_user_id, bot, "bind", [row])
        logger.info(
            "Resource binding confirmed: platform=%s bot=%s binding=%s",
            platform,
            bot.id,
            credentials.short_id(record.binding_id),
        )
        reply = t(
            "binding.confirmed",
            lang=lang,
            binding_id=credentials.short_id(record.binding_id),
            bot_id=bot.id,
        )
    except Exception:
        # The durable write (or building its reply) failed before commit; hand the code back
        # so the same confirmation can be retried rather than being silently consumed.
        pending_store.restore(token)
        raise
    return reply, token


def _binding_is_current(row, bot, now) -> bool:
    return (
        row.bound_bot_uuid == (bot.uuid or "")
        and row.bound_identity_revision == bot.identity_revision
        and (row.credential_expires_at is None or row.credential_expires_at > _naive_utc(now))
    )


def _own_bindings(db: Session, platform: str, external_user_id: str, bot_id: int):
    return (
        db.query(ResourceBinding)
        .filter(
            ResourceBinding.platform == platform,
            ResourceBinding.external_user_id == external_user_id,
            ResourceBinding.bound_bot_id == bot_id,
            ResourceBinding.revoked_at.is_(None),
        )
        .order_by(ResourceBinding.issued_at)
        .all()
    )


def _chat_list(db: Session, platform: str, external_user_id: str, bot, lang: str) -> str:
    rows = [r for r in _own_bindings(db, platform, external_user_id, bot.id) if _binding_is_current(r, bot, _utcnow())]
    if not rows:
        return _with_agent_usage_hint(db, bot, lang, t("binding.list_empty", lang=lang, bot_id=bot.id))

    lines = [t("binding.list_header", lang=lang)]
    for row in rows:
        lines.append(
            t(
                "binding.list_item",
                lang=lang,
                binding_id=credentials.short_id(row.public_id),
                label=row.label or "-",
                bot_id=bot.id if bot else row.bound_bot_id,
                last_used=row.last_used_at.isoformat(sep=" ", timespec="seconds") if row.last_used_at else "-",
            )
        )
    return "\n".join(lines)


def _chat_unbind(db: Session, platform: str, external_user_id: str, bot, lang: str, argument: str) -> str:
    target = (argument or "").strip()
    rows = _own_bindings(db, platform, external_user_id, bot.id)
    now = _naive_utc(_utcnow())

    if not target or target.lower() == "all":
        had_current = any(_binding_is_current(row, bot, now) for row in rows)
        for row in rows:
            row.revoked_at = now
        _audit_binding_change(db, platform, external_user_id, bot, "unbind", rows)
        key = "binding.revoked_current" if had_current else "binding.nothing_to_revoke"
        reply = t(key, lang=lang, bot_id=bot.id)
        return reply if had_current else _with_agent_usage_hint(db, bot, lang, reply)

    matches = [r for r in rows if credentials.short_id(r.public_id) == target]
    if not matches:
        return t("binding.not_found", lang=lang, binding_id=target)
    for row in matches:
        row.revoked_at = now
    _audit_binding_change(db, platform, external_user_id, bot, "unbind", matches)
    return t("binding.revoked_one", lang=lang, binding_id=target)
