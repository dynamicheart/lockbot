"""Additive, transactionally mirrored application credentials.

Existing provider APIs remain the write/validation boundary. A known registry row
never falls back to legacy fields after corruption, expiry or unsupported schema.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, StrictStr, ValidationError
from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, event, inspect, select
from sqlalchemy.orm import Mapped, Session, mapped_column, object_session

from lockbot.backend.app.bots import encryption
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import Base
from lockbot.backend.app.feishu.models import FeishuApplication


class PlatformApplication(Base):
    __tablename__ = "platform_applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    stable_identity: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    name_from_source: Mapped[bool] = mapped_column(Boolean, default=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    encrypted_payload: Mapped[str] = mapped_column(Text)
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("provider", "stable_identity", name="uq_platform_application_identity"),)
    __mapper_args__ = {"version_id_col": generation}


class InfoflowCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: Literal["infoflow"]
    webhook_url: StrictStr
    token: SecretStr
    aes_key: SecretStr


class FeishuCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: Literal["feishu"]
    app_id: StrictStr
    app_secret: SecretStr
    verification_token: SecretStr
    encrypt_key: SecretStr


class CredentialUnavailable(ValueError):
    """Safe error text only: validation/decryption exceptions can contain credentials."""


_FIELDS = {
    "infoflow": ("webhook_url", "token", "aes_key"),
    "feishu": ("app_secret", "verification_token", "encrypt_key"),
}


def source_identity(source):
    if isinstance(source, FeishuApplication):
        return "feishu", source.app_id
    if isinstance(source, Bot) and (source.platform or "infoflow").lower() == "infoflow":
        return "infoflow", source.uuid
    return None, None


def fingerprint(source, provider):
    encoded = json.dumps([(getattr(source, key) or "") for key in _FIELDS[provider]], separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _validate(provider, value):
    try:
        model = {"infoflow": InfoflowCredentials, "feishu": FeishuCredentials}[provider]
        result = model.model_validate(value)
        # SecretStr rejects non-strings, and provider fields are not interchangeable.
        plain = {key: val.get_secret_value() if isinstance(val, SecretStr) else val for key, val in result}
        if any(len(item) > 8192 for item in plain.values()):
            raise ValueError()
        return plain
    except (ValidationError, KeyError, TypeError, ValueError):
        raise CredentialUnavailable("Messaging credentials are invalid or use an unsupported schema") from None


def legacy_payload(source, provider):
    try:
        values = {key: encryption.decrypt(getattr(source, key)) for key in _FIELDS[provider]}
        if provider == "feishu":
            values["app_id"] = source.app_id
        return _validate(provider, {"provider": provider, **values})
    except Exception:
        raise CredentialUnavailable("Messaging credentials could not be decrypted or validated") from None


def _decode(row):
    if row.schema_version != 1 or row.provider not in _FIELDS:
        raise CredentialUnavailable("Unsupported messaging credential schema; administrator action required")
    if row.expires_at is not None and row.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
        raise CredentialUnavailable("Messaging credentials have expired; administrator action required")
    try:
        value = json.loads(encryption.decrypt(row.encrypted_payload))
    except Exception:
        raise CredentialUnavailable("Messaging credential payload is unavailable") from None
    result = _validate(row.provider, value)
    if row.provider == "feishu" and result["app_id"] != row.stable_identity:
        raise CredentialUnavailable("Messaging application identity mismatch")
    return result


def find_record(db, provider, identity):
    # Include pending rows so multiple flush participants cannot create duplicates.
    for row in db.new:
        if isinstance(row, PlatformApplication) and (row.provider, row.stable_identity) == (provider, identity):
            return row
    row = db.execute(select(PlatformApplication).filter_by(stable_identity=identity)).scalar_one_or_none()
    if row is not None and row.provider != provider:
        raise CredentialUnavailable("Messaging application provider mismatch")
    return row


def credentials_for(source):
    provider, identity = source_identity(source)
    if provider is None:
        if isinstance(source, Bot) and (source.platform or "").lower() == "feishu":
            # Feishu Bot placeholders carry no application credential authority.
            return {"webhook_url": "", "aes_key": "", "token": ""}
        raise CredentialUnavailable("Messaging credential source must be a current provider record")
    db = object_session(source)
    if db is None and getattr(source, "id", None) is not None:
        raise CredentialUnavailable("Messaging application must be reloaded from its current database session")
    row = find_record(db, provider, identity) if db is not None and identity else None
    if row is None:
        return legacy_payload(source, provider)
    value = _decode(row)
    if row.source_fingerprint != fingerprint(source, provider):
        raise CredentialUnavailable("Messaging credential layouts disagree; refresh or reconcile before use")
    return value


def bot_credentials(source):
    """Keep legacy adapter-extension/dry-run config inputs outside the known-provider registry.

    Real managed Infoflow rows always pass through current registry validation.
    Feishu Bot placeholders never become an application credential source.
    """
    if isinstance(source, Bot) and (source.platform or "infoflow").lower() in {"infoflow", "feishu"}:
        return credentials_for(source)
    return {key: encryption.decrypt(getattr(source, key)) for key in _FIELDS["infoflow"]}


def mirror_source(db, source):
    provider, identity = source_identity(source)
    if not identity:
        return False
    row = find_record(db, provider, identity)
    if row is not None:
        _decode(row)  # Unknown/expired layouts must not be overwritten into apparent validity.
    value = legacy_payload(source, provider)
    encoded = json.dumps(value, separators=(",", ":"))
    digest = fingerprint(source, provider)
    if row is None:
        db.add(
            PlatformApplication(
                provider=provider,
                stable_identity=identity,
                name=source.name,
                schema_version=1,
                encrypted_payload=encryption.encrypt(encoded),
                source_fingerprint=digest,
            )
        )
        return True
    newly_referenced = source in db.new
    changed = newly_referenced or row.source_fingerprint != digest or (row.name_from_source and row.name != source.name)
    if changed:
        if newly_referenced:
            row.generation += 1  # Serialize orphan deletion against source creation/restore.
        if row.name_from_source:
            row.name = source.name
        row.encrypted_payload = encryption.encrypt(encoded)
        row.source_fingerprint = digest
    return changed


@event.listens_for(Session, "before_flush")
def mirror_provider_changes(db, flush_context, instances):
    """One transaction for legacy writes and versioned storage; failures roll both back."""
    for source in list(db.new) + list(db.dirty):
        provider, identity = source_identity(source)
        if not identity:
            continue
        changed = source in db.new or any(
            inspect(source).attrs[key].history.has_changes() for key in (*_FIELDS[provider], "name")
        )
        if changed:
            mirror_source(db, source)
    for source in list(db.deleted):
        provider, identity = source_identity(source)
        if identity:
            row = find_record(db, provider, identity)
            if row is not None:
                db.delete(row)


def backfill(factory, *, limit=None, after=None, strict=True):
    """Restartable per-source commits. Never alter bot, route or authorization identity.

    `after` is only a test/operational checkpoint hook; no secret values are passed.
    """
    count = 0
    for model in (FeishuApplication, Bot):
        with factory() as db:
            ids = [item[0] for item in db.execute(select(model.id).order_by(model.id))]
        for source_id in ids:
            with factory() as db:
                source = db.get(model, source_id)
                try:
                    if source is not None and mirror_source(db, source):
                        db.commit()
                        count += 1
                except CredentialUnavailable:
                    if strict:
                        raise
                    logging.getLogger(__name__).error(
                        "Messaging credential migration requires administrator repair: %s #%d",
                        model.__tablename__,
                        source_id,
                    )
            if after:
                after(model.__tablename__, source_id)
            if limit is not None and count >= limit:
                return count
    return count
