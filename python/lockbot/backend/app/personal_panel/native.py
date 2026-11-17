"""Read-only personal panel provider using the original service's confirmed bindings.

Grants contain no keys or credential digests. Without an administrator-attested
app/tenant namespace, identities remain isolated by deployment and bot UUID.
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from lockbot.backend.app.bots.datasource import public_datasource
from lockbot.backend.app.bots.manager import bot_manager
from lockbot.backend.app.bots.metrics_access import issue_metrics_access
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.bots.node_access import attach_node_access, saved_node_access
from lockbot.backend.app.database import SessionLocal
from lockbot.backend.app.resource_api.snapshot import build_resources
from lockbot.backend.app.resource_api.usage import TASK_CATEGORIES, UsageDeclaration, holding_proof
from lockbot.backend.app.resource_api.view_links import verify_view, window_expiry
from lockbot.backend.app.resource_auth import credentials, service
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
                        allocation = {
                            "kind": kind,
                            "node": node["id"],
                            "device": identity[1],
                            "expires_at": entry.get("expires_at"),
                        }
                        if kind == "reservation" and "position" in entry:
                            allocation["position"] = entry["position"]
                        if kind == "hold" and "node_access" in unit:
                            allocation["node_access"] = unit["node_access"]
                        if kind == "hold" and "resource_guidance" in unit:
                            allocation["resource_guidance"] = unit["resource_guidance"]
                        allocations.append(allocation)
    return allocations, complete


def _attach_own_tasks(db, binding, captured, state, allocations):
    holds = {}
    for allocation in allocations:
        if allocation["kind"] != "hold":
            continue
        node, device = allocation["node"], allocation["device"]
        resource_id = node if device is None else f"{node}:dev{device}"
        holds[resource_id] = allocation
        allocation["own_task"] = None
    if not holds:
        return
    active = (
        db.query(UsageDeclaration)
        .filter(
            UsageDeclaration.bot_id == binding.bound_bot_id,
            UsageDeclaration.binding_id == binding.id,
            UsageDeclaration.expires_at > captured.replace(tzinfo=None),
        )
        .yield_per(128)
    )
    for note in active:
        if note.resource_id not in holds:
            continue
        if note.holding_proof != holding_proof(
            state, note.resource_id, binding.external_user_id, now=captured.timestamp()
        ):
            continue
        holds[note.resource_id]["own_task"] = {
            "category": TASK_CATEGORIES.get(note.task_category),
            "note": note.purpose,
            "agent_label": note.agent_label,
        }


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
        self.guidance = None

    def owns_handoff_grant(self, grant):
        # Reserve a separate realm for local-Key sign-ins in an external panel.
        # The exact binding still has to pass validate() against the local DB.
        realm = grant.get("realm")
        return (
            grant.get("issuer") == self.config["issuer"]
            and isinstance(realm, str)
            and realm.startswith('["local-key-handoff",')
        )

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
        if self.config.get("local_key_handoff") is True:
            realm = _namespace("local-key-handoff", realm)
        generation = row.credential_generation
        if not isinstance(generation, int) or generation < 0:
            raise ProviderError("credential_invalid")
        scope = _namespace(instance, bot.uuid, bot.identity_revision)
        if generation:
            scope = _namespace(instance, bot.uuid, bot.identity_revision, "credential_generation", generation)
        return {
            "issuer": self.config["issuer"],
            "platform": row.platform,
            "realm": realm,
            "subject": row.external_user_id,
            "scope": scope,
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

    def _account_namespace(self, db, row):
        from lockbot.backend.app.resource_api.discovery import _binding_namespace

        pin = self.config.get("trusted_realms", {}).get(row.bound_bot_uuid)
        realm = pin.get("realm") if isinstance(pin, dict) else None
        return _binding_namespace(db, row.bound_bot_id, row.platform), realm

    def recover_panel_grants(self, seed):
        """Prove other bot bindings using their own recoverable credentials."""
        with self.session_factory() as db:
            row = self._validated(db, seed)
            namespace = self._account_namespace(db, row)
            candidates = (
                db.query(ResourceBinding)
                .filter(
                    ResourceBinding.platform == row.platform,
                    ResourceBinding.external_user_id == row.external_user_id,
                    ResourceBinding.bound_bot_id != row.bound_bot_id,
                    ResourceBinding.revoked_at.is_(None),
                )
                .order_by(ResourceBinding.bound_bot_id, ResourceBinding.id)
            )
            recovered, seen = [], {row.bound_bot_id}
            for binding in candidates:
                if binding.bound_bot_id in seen or self._account_namespace(db, binding) != namespace:
                    continue
                key = credentials.recover_credential(binding)
                if key is None:
                    continue
                authenticated = service.authenticate_for_any_bot(db, token=key)
                if authenticated is None or authenticated.id != binding.id:
                    continue
                try:
                    recovered.append(self._grant(db, authenticated))
                except ProviderError:
                    continue
                seen.add(binding.bound_bot_id)
            return recovered

    def panel_account_groups(self, grants):
        """Recheck each grant and its IM namespace before showing or admitting it."""
        with self.session_factory() as db:
            primary = self._validated(db, grants[0])
            namespace = self._account_namespace(db, primary)
            names = [db.get(Bot, primary.bound_bot_id).name]
            for grant in grants[1:]:
                row = self._validated(db, grant)
                if (row.platform, row.external_user_id) != (
                    primary.platform,
                    primary.external_user_id,
                ) or self._account_namespace(db, row) != namespace:
                    raise ProviderError("identity_namespace_mismatch")
                names.append(db.get(Bot, row.bound_bot_id).name)
            return names

    def connection(self, grant):
        """Inspect exactly one proven native binding, never enumerate its user's bots."""
        with self.session_factory() as db:
            row = self._validated(db, grant)
            with authority_guard(row.bound_bot_id, platform=row.platform, external_user_id=row.external_user_id):
                db.rollback()
                db.expire_all()
                row = self._validated(db, grant)
                bot = db.get(Bot, row.bound_bot_id)
                health = (
                    "legacy_missing"
                    if row.credential_encrypted is None
                    else "recoverable"
                    if credentials.recover_credential(row) is not None
                    else "unreadable"
                )
                expiry = row.credential_expires_at
                if expiry is not None:
                    expiry = (
                        expiry.replace(tzinfo=timezone.utc)
                        if expiry.tzinfo is None
                        else expiry.astimezone(timezone.utc)
                    )
                from lockbot.backend.app.resource_api.activity import binding_label_activity

                value = {
                    "instance_id": self._instance_id(db),
                    "bot": {"id": bot.id, "uuid": bot.uuid, "name": bot.name, "type": bot.bot_type},
                    "actor": {"platform": row.platform, "external_user_id": row.external_user_id},
                    "identity_revision": bot.identity_revision,
                    "expires_at": expiry.isoformat() if expiry is not None else None,
                    "credential_recovery_health": health,
                    "agent_activity": binding_label_activity(db, row.id, include_items=True),
                }
                # Decryption may be slow. Re-read authority even if another DB
                # writer did not participate in this process's guard.
                db.rollback()
                db.expire_all()
                self._validated(db, grant)
                return value

    @staticmethod
    def _credential_pin(row):
        return hashlib.sha256(row.credential_digest.encode()).hexdigest()

    def issue_credential_view(self, key):
        """Original native bearer proof only; no cookie or read-only grant upgrade."""
        if not isinstance(key, str) or not key.startswith("lbr_"):
            raise ProviderError("credential_invalid")
        grant = self.authenticate(key)
        with self.session_factory() as db:
            row = self._validated(db, grant)
            with authority_guard(row.bound_bot_id, platform=row.platform, external_user_id=row.external_user_id):
                db.rollback()
                db.expire_all()
                row = self._validated(db, grant)
                if not credentials.credential_matches(row.credential_digest, key):
                    raise ProviderError("credential_invalid")
                expiry = row.credential_expires_at
                if expiry is not None:
                    expiry = expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry
                return grant, self._credential_pin(row), expiry.timestamp() if expiry else None

    def credential_view(self, grant, pin, *, reveal=False):
        """Recheck the original binding and current key before and after decryption."""
        with self.session_factory() as db:
            row = self._validated(db, grant)
            with authority_guard(row.bound_bot_id, platform=row.platform, external_user_id=row.external_user_id):
                db.rollback()
                db.expire_all()
                row = self._validated(db, grant)
                if not hmac.compare_digest(self._credential_pin(row), pin):
                    raise ProviderError("credential_changed")
                key = credentials.recover_credential(row) if reveal else None
                if reveal and key is None:
                    raise ProviderError("credential_recovery_unavailable")
                db.rollback()
                db.expire_all()
                row = self._validated(db, grant)
                if not hmac.compare_digest(self._credential_pin(row), pin):
                    raise ProviderError("credential_changed")
                return key

    def rotate_credential(self, grant, pin, canonical_name, deadline):
        """Use a confirmed rotation intent against exact current authority."""
        with self.session_factory() as db:
            row = self._validated(db, grant)
            with authority_guard(row.bound_bot_id, platform=row.platform, external_user_id=row.external_user_id):
                db.rollback()
                db.expire_all()
                row = self._validated(db, grant)
                if not hmac.compare_digest(self._credential_pin(row), pin):
                    raise ProviderError("credential_changed")
                return service.rotate_confirmed_credential(
                    db, row=row, instance_id=self._instance_id(db), canonical_name=canonical_name, deadline=deadline
                )

    def utilization(self, grant, *, session_expiry=None, authorize=False):
        """Describe or authorize telemetry for the exact native bot binding."""
        if self.config.get("enable_utilization") is not True:
            return None
        allocations = []
        if authorize:
            (group,) = self.report(grant)
            if group["status"] != "ok":
                return None
            allocations = [row for row in group["allocations"] if row["kind"] == "hold"]
        with self.session_factory() as db:
            row = self._validated(db, grant)
            bot = db.get(Bot, row.bound_bot_id)
            source = public_datasource(bot)
            if not source or source["accessMode"] != "short_lived" or source["profile"] != "lockbot-v1":
                return None
            if not authorize:
                return {"configuration": source}
            # An allocation without a confirmed deadline cannot authorize telemetry.
            # Keep its node out of the grant even when other holdings are valid.
            deadlines = {}
            holdings = {}
            device_bot = bot.bot_type in {"DEVICE", "QUEUE_DEVICE"}
            now = datetime.now(timezone.utc)
            for holding in allocations:
                node = holding["node"]
                device = holding.get("device")
                expires_at = holding.get("expires_at")
                if node not in source["nodeMapping"] or not isinstance(expires_at, str):
                    continue
                if device_bot != (device is not None):
                    continue
                try:
                    deadline = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if deadline.tzinfo is None or deadline <= now:
                    continue
                unit = (node, device)
                deadlines[unit] = min(deadlines.get(unit, deadline), deadline)
                if device_bot:
                    holdings.setdefault(node, set()).add(device)
                else:
                    holdings[node] = None
            if not deadlines or session_expiry is None:
                return None
            expiry = min(session_expiry, *deadlines.values())
            if row.credential_expires_at is not None:
                credential_expiry = row.credential_expires_at
                if credential_expiry.tzinfo is None:
                    credential_expiry = credential_expiry.replace(tzinfo=timezone.utc)
                expiry = min(expiry, credential_expiry)
            access = issue_metrics_access(db, bot, source, expires_at=expiry, holdings=holdings)
            return {"configuration": source, "access": access} if access else None

    def shared_node_access(self, grant, bot_id, token):
        with self.session_factory() as db:
            binding = self._validated(db, grant)
            if binding.bound_bot_id != bot_id or verify_view(db, db.get(Bot, bot_id), token) is None:
                raise ProviderError("shared_view_unavailable")
            with (
                bot_manager.admit(bot_id, for_read=True) as lease,
                authority_guard(bot_id, platform=binding.platform, external_user_id=binding.external_user_id),
            ):
                db.rollback()
                db.expire_all()
                row = self._validated(db, grant)
                bot = db.get(Bot, row.bound_bot_id)
                window = verify_view(db, bot, token) if row.bound_bot_id == bot_id else None
                if window is None:
                    raise ProviderError("shared_view_unavailable")
                addresses = saved_node_access(bot)
                if (lease.health or bot.status) != "running" or not addresses:
                    return {}
                captured, state = lease.bot.state_snapshot()
                now = datetime.now(timezone.utc)
                if not -30 <= (now - captured).total_seconds() <= 60:
                    return {}
                resources = build_resources(
                    captured_at=captured,
                    state=state,
                    bot_type=bot.bot_type,
                    platform=row.platform,
                    caller_id=row.external_user_id,
                )
                attach_node_access(resources, addresses, row.external_user_id, now)
                allocations, complete = _allocations(resources, row.external_user_id)
                if not complete:
                    return {}
                deadline = window_expiry(window)
                if deadline is not None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                if row.credential_expires_at is not None:
                    expiry = row.credential_expires_at
                    expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
                    deadline = min(deadline, expiry) if deadline else expiry
                result = {}
                for hold in allocations:
                    if "node_access" not in hold or not hold["expires_at"]:
                        continue
                    expires = datetime.fromisoformat(hold["expires_at"])
                    expires = min(expires, deadline) if deadline else expires
                    node = hold["node"]
                    if node not in result or expires > result[node]["expires_at"]:
                        result[node] = {"address": hold["node_access"], "expires_at": expires}
                db.rollback()
                db.expire_all()
                self._validated(db, grant)
                if verify_view(db, db.get(Bot, bot_id), token) is None:
                    raise ProviderError("shared_view_unavailable")
                return result

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
                    now = datetime.now(timezone.utc)
                    age = (now - captured).total_seconds()
                    healthy = (lease.health or bot.status) == "running"
                    if healthy and -30 <= age <= 60:
                        attach_node_access(resources, saved_node_access(bot), row.external_user_id, now)
                        if self.guidance is not None:
                            self.guidance.attach(resources, self._instance_id(db), bot.uuid, grant, now)
                    allocations, complete = _allocations(resources, row.external_user_id)
                    known = complete and healthy and -30 <= age <= 60
                    if not known:
                        for allocation in allocations:
                            allocation.pop("node_access", None)
                            allocation.pop("resource_guidance", None)
                    if known:
                        _attach_own_tasks(db, row, captured, state, allocations)
                    # Read a new transaction even if another database writer bypassed
                    # this process's authority guard while the snapshot was assembled.
                    db.rollback()
                    db.expire_all()
                    self._validated(db, grant)
                    return [
                        {
                            "label": bot.name,
                            "group_id": group_id,
                            "status": "ok" if known else "unknown",
                            "allocations": allocations,
                            **(
                                {
                                    "capacity": {
                                        "nodes": len(resources),
                                        "cards": sum(len(node.get("devices", [])) for node in resources)
                                        if all("devices" in node for node in resources)
                                        else None,
                                    }
                                }
                                if known
                                else {}
                            ),
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
