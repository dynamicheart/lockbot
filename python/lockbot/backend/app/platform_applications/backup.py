"""Carry encrypted schema evidence across restore without integer-ID coupling."""

import json

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
        "SELECT provider, schema_version, encrypted_payload FROM platform_applications WHERE stable_identity = ?",
        (identity,),
    ).fetchone()
    return dict(row) if row else None


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
