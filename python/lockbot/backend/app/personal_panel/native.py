"""Read-only personal panel provider using the original service's confirmed bindings.

Grants contain no keys or credential digests. Without an administrator-attested
app/tenant namespace, identities remain isolated by deployment and bot UUID.
"""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from lockbot.backend.app.bots.manager import bot_manager
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import SessionLocal
from lockbot.backend.app.resource_api.snapshot import build_resources
from lockbot.backend.app.resource_auth import service
from lockbot.backend.app.resource_auth.authority import authority_guard
from lockbot.backend.app.resource_auth.models import ResourceBinding
from lockbot.backend.app.settings.models import SiteSetting

FIELDS = frozenset(("issuer", "platform", "realm", "subject", "scope", "ref"))


class ProviderError(ValueError):
    """A public, credential-free provider failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _namespace(*parts):
    return json.dumps(parts, ensure_ascii=True, separators=(",", ":"))


def _future(value):
    if value is None:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value > datetime.now(timezone.utc)


def _allocations(resources, subject):
    """Filter the authoritative snapshot and retain gaps as incomplete coverage."""
    allocations, seen = [], set()
    complete = isinstance(resources, list)
    for node in resources if complete else []:
        if not isinstance(node, dict) or not _text(node.get("id")):
            complete = False
            continue
        devices = node.get("devices")
        if "devices" in node and (not isinstance(devices, list) or not devices):
            complete = False
            continue
        for unit in devices if devices is not None else [node]:
            if not isinstance(unit, dict):
                complete = False
                continue
            device = unit.get("id") if devices is not None else None
            identity = (node["id"], str(device) if device is not None else None)
            if identity in seen or (devices is not None and device is None):
                complete = False
            seen.add(identity)
            for kind, field in (("hold", "holders"), ("reservation", "booking_list")):
                entries = unit.get(field, [] if kind == "reservation" else None)
                if not isinstance(entries, list):
                    complete = False
                    continue
                for entry in entries:
                    if not isinstance(entry, dict) or not _text(entry.get("external_user_id")):
                        complete = False
                        continue
                    if entry["external_user_id"] == subject:
                        allocations.append(
                            {
                                "kind": kind,
                                "node": node["id"],
                                "device": identity[1],
                                "expires_at": entry.get("expires_at"),
                            }
                        )
    return allocations, complete


class NativeProvider:
    """Use the existing issuer's database and runtime; never widen a binding's bot scope.

    Config requires ``issuer``. Optional ``trusted_realms`` maps stable bot UUIDs
    to administrator-attested ``{"platform": ..., "realm": ...}`` namespaces.
    Those attestations must identify verified IM app/tenant environments, not
    editable bot settings or values supplied during login. ``session_factory``
    supports embedding and tests; it defaults to the original ``SessionLocal``.
    """

    def __init__(self, config):
        if not _text(config.get("issuer")):
            raise ValueError("provider issuer must be explicitly configured")
        self.config = config
        self.session_factory = config.get("session_factory") or SessionLocal

    @staticmethod
    def _instance_id(db: Session):
        row = db.get(SiteSetting, "resource_api_instance_id")
        # A read-only provider must not mint a new deployment identity on login.
        if row is None or not _text(row.value):
            raise ProviderError("instance_identity_unavailable")
        return row.value

    def _grant(self, db: Session, row: ResourceBinding | None):
        if row is None or row.revoked_at is not None or not _future(row.credential_expires_at):
            raise ProviderError("credential_invalid")
        bot = db.get(Bot, row.bound_bot_id)
        if (
            bot is None
            or bot.is_deleted
            or not bot.resource_auth_enabled
            or not _text(bot.uuid)
            or row.bound_bot_uuid != bot.uuid
            or row.bound_identity_revision != bot.identity_revision
            or not _text(row.platform)
            or not _text(row.external_user_id)
            or not _text(row.public_id)
        ):
            raise ProviderError("credential_invalid")
        instance = self._instance_id(db)
        realm = _namespace("instance", instance, "bot", bot.uuid)
        pin = self.config.get("trusted_realms", {}).get(bot.uuid)
        if pin is not None:
            if not isinstance(pin, dict) or pin.get("platform") != row.platform or not _text(pin.get("realm")):
                raise ProviderError("identity_namespace_mismatch")
            realm = _namespace("instance", instance, "attested", pin["realm"])
        return {
            "issuer": self.config["issuer"],
            "platform": row.platform,
            "realm": realm,
            "subject": row.external_user_id,
            "scope": _namespace(instance, bot.uuid, bot.identity_revision),
            "ref": row.public_id,
        }

    def authenticate(self, key):
        if not _text(key):
            raise ProviderError("credential_invalid")
        with self.session_factory() as db:
            row = service.authenticate_for_any_bot(db, token=key)
            if row is None:
                raise ProviderError("credential_invalid")
            with authority_guard(row.bound_bot_id, platform=row.platform, external_user_id=row.external_user_id):
                db.rollback()
                db.expire_all()
                return self._grant(db, service.authenticate_for_any_bot(db, token=key))

    def _validated(self, db: Session, grant):
        if not isinstance(grant, dict) or set(grant) != FIELDS or any(not _text(v) for v in grant.values()):
            raise ProviderError("credential_invalid")
        row = db.query(ResourceBinding).filter(ResourceBinding.public_id == grant["ref"]).one_or_none()
        if self._grant(db, row) != grant:
            raise ProviderError("credential_invalid")
        return row

    def validate(self, grant):
        try:
            with self.session_factory() as db:
                self._validated(db, grant)
            return True
        except Exception:
            return False

    def report(self, grant):
        with self.session_factory() as db:
            row = self._validated(db, grant)
            bot = db.get(Bot, row.bound_bot_id)
            label = bot.name
            group_id = _namespace(self._instance_id(db), bot.uuid)
            try:
                with (
                    bot_manager.admit(bot.id, for_read=True) as lease,
                    authority_guard(bot.id, platform=row.platform, external_user_id=row.external_user_id),
                ):
                    db.rollback()
                    db.expire_all()
                    row = self._validated(db, grant)
                    bot = db.get(Bot, row.bound_bot_id)
                    captured, state = lease.bot.state_snapshot()
                    resources = build_resources(
                        captured_at=captured,
                        state=state,
                        bot_type=bot.bot_type,
                        platform=row.platform,
                        caller_id=row.external_user_id,
                    )
                    allocations, complete = _allocations(resources, row.external_user_id)
                    age = (datetime.now(timezone.utc) - captured).total_seconds()
                    healthy = (lease.health or bot.status) == "running"
                    # Read a new transaction even if another database writer bypassed
                    # this process's authority guard while the snapshot was assembled.
                    db.rollback()
                    db.expire_all()
                    self._validated(db, grant)
                    known = complete and healthy and -30 <= age <= 60
                    return [
                        {
                            "label": bot.name,
                            "group_id": group_id,
                            "status": "ok" if known else "unknown",
                            "allocations": allocations,
                            "source_at": captured.isoformat(),
                            **({"reason": "stale_or_incomplete_report"} if not known else {}),
                        }
                    ]
            except ProviderError:
                raise
            except Exception:
                db.rollback()
                db.expire_all()
                self._validated(db, grant)
                return [
                    {
                        "label": label,
                        "group_id": group_id,
                        "status": "unknown",
                        "allocations": [],
                        "reason": "report_unavailable",
                    }
                ]


def create_provider(config):
    return NativeProvider(config)
