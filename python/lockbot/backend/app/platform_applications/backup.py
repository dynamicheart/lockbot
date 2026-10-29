"""Carry encrypted schema evidence across restore without integer-ID coupling."""

import json
import re
from datetime import datetime, timezone

from lockbot.backend.app.platform_applications.service import _validate


def archived_envelope(connection, identity):
    if not identity:
        return None
    present = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_applications'"
    ).fetchone()
    if not present:
        return None
    row = connection.execute(
        "SELECT provider, schema_version, encrypted_payload, expires_at "
        "FROM platform_applications WHERE stable_identity = ?",
        (identity,),
    ).fetchone()
    return dict(row) if row else None


def archived_expiry(archived, *, allow_expired=False):
    """An explicit null means non-expiring; missing/malformed evidence is not null."""
    if "expires_at" not in archived:
        raise ValueError("Archived messaging credential expiry is missing")
    value = archived["expires_at"]
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?",
        value,
    ):
        raise ValueError("Archived messaging credential expiry is invalid")
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        expiry = expiry.replace(tzinfo=expiry.tzinfo or timezone.utc).astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise ValueError("Archived messaging credential expiry is invalid") from None
    if not allow_expired and expiry <= datetime.now(timezone.utc).replace(tzinfo=None):
        raise ValueError("Archived messaging credentials have expired")
    return expiry


def set_imported_expiry(db, source, expiry, *, legacy_archive=False):
    from lockbot.backend.app.platform_applications.service import _decode, find_record, mirror_source, source_identity

    if expiry is not None and expiry <= datetime.now(timezone.utc).replace(tzinfo=None):
        raise ValueError("Archived messaging credentials have expired")
    provider, identity = source_identity(source)
    row = find_record(db, provider, identity)
    if row is None:
        mirror_source(db, source)
        row = find_record(db, provider, identity)
    if not legacy_archive or row.expires_at is None:
        row.expires_at = expiry
    _decode(row)  # Never conceal an unsupported or corrupt target envelope.


def validate_archived(provider, archived, source_key, expected):
    if archived is None:
        return  # Legacy archive: provider-specific source fields remain the supported import.
    if (
        not isinstance(archived, dict)
        or archived.get("provider") != provider
        or type(archived.get("schema_version")) is not int
        or archived["schema_version"] != 1
    ):
        raise ValueError("Unsupported archived messaging credential schema")
    expiry = archived_expiry(archived)
    from lockbot.backend.app.backup.service import _decrypt_field

    plain, valid = _decrypt_field(archived.get("encrypted_payload", ""), source_key)
    if not valid:
        raise ValueError("Cannot decrypt archived messaging credential envelope")
    try:
        payload = _validate(provider, json.loads(plain))
    except (ValueError, TypeError):
        raise ValueError("Invalid archived messaging credential envelope") from None
    if payload != expected:
        raise ValueError("Archived messaging credential layouts disagree")

    return expiry
