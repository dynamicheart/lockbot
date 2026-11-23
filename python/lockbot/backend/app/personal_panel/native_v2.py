"""Read-only browser access backed by the same grants and snapshots as lb."""

import json
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import HTTPException

from lockbot.backend.app.database import SessionLocal
from lockbot.backend.app.resource_api.v2.binding import (
    PersonalCredential,
    active_resource_grants,
    api_error,
    authenticate,
    is_expired,
)
from lockbot.backend.app.resource_api.v2.identity import CARD_BOTS, config_units
from lockbot.backend.app.resource_api.v2.resources import query_resource_page

from .native import ProviderError


class NativeV2Provider:
    def __init__(self, issuer, *, session_factory=None):
        self.issuer = issuer
        self.session_factory = session_factory or SessionLocal

    def _proof(self, principal):
        return {
            "issuer": self.issuer,
            "platform": principal.platform,
            "realm": self._realm(principal.namespace),
            "subject": principal.external_user_id,
            "scope": str(principal.generation),
            "ref": principal.public_id,
        }

    @staticmethod
    def _realm(namespace):
        return json.dumps(["native-v2", namespace], separators=(",", ":"))

    def owns(self, proof):
        realm = proof.get("realm")
        return proof.get("issuer") == self.issuer and isinstance(realm, str) and realm.startswith('["native-v2",')

    def _validated(self, db, proof):
        principal = db.get(PersonalCredential, proof.get("ref"))
        if principal is None or is_expired(principal) or self._proof(principal) != proof:
            raise ProviderError("credential_invalid")
        return principal

    def authenticate(self, key):
        with self.session_factory() as db:
            principal = authenticate(db, "Bearer " + key)
            return self._proof(principal) if principal is not None else None

    def validate(self, proof):
        with self.session_factory() as db:
            try:
                self._validated(db, proof)
            except ProviderError:
                return False
        return True

    def query(self, proof, **filters):
        def load_principal(db):
            try:
                return self._validated(db, proof)
            except ProviderError:
                raise api_error("credential_invalid", 401) from None

        with self.session_factory() as db:
            return query_resource_page(db, load_principal, **filters)

    def report(self, proof):
        with self.session_factory() as db:
            groups = self._groups(db, proof)
        if not groups:
            return []
        allocations = defaultdict(list)
        unavailable = set()
        cursor = None
        # A successful page advances by at least one row. The configured
        # inventory bounds traversal even while other clients change allocations.
        for _ in range(max(1, sum(group["units"] for group in groups.values()))):
            try:
                page = self.query(proof, mine=True, limit=100, cursor=cursor, charge_budget=False)
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                unavailable.update(groups)
                break
            unavailable.update(page["unavailable"])
            if page["unavailable_count"] > len(page["unavailable"]):
                unavailable.update(groups)
            for row in page["rows"]:
                allocations[row["grant_ref"]].extend(_allocations(row))
            cursor = page["next_cursor"]
            if cursor is None:
                break
        if cursor is not None:
            unavailable.update(groups)
        with self.session_factory() as db:
            current = self._groups(db, proof)
        source_at = datetime.now(timezone.utc).isoformat()
        return [
            {
                "label": group["name"],
                "group_id": ref,
                "status": "unknown" if ref in unavailable else "ok",
                "allocations": [] if ref in unavailable else allocations[ref],
                "capacity": group["capacity"],
                "source_at": source_at,
            }
            for ref, group in groups.items()
            if current.get(ref) == group
        ]

    def _groups(self, db, proof):
        groups = {}
        for grant, bot in active_resource_grants(db, self._validated(db, proof)):
            units = config_units(bot)
            groups[grant.public_id] = {
                "name": bot.name,
                "confirmed_at": grant.confirmed_at,
                "revision": bot.identity_revision,
                "bot_type": bot.bot_type,
                "cluster_configs": bot.cluster_configs,
                "units": len(units),
                "capacity": {
                    "nodes": len({node for node, _ in units}),
                    "cards": len(units) if bot.bot_type in CARD_BOTS else None,
                },
            }
        return groups


def _allocations(row):
    resource = {
        "node": row["node"],
        "device": str(row["device"]) if row["device"] is not None else None,
        "resource_ref": row["resource_ref"],
        "status": row["status"],
        "hardware": row["hardware"],
        "metrics": row["metrics"],
        "label": row["label"],
    }
    if row.get("holding"):
        yield {"kind": "hold", **resource, **row["holding"]}
    if row.get("booking"):
        yield {"kind": "reservation", **resource, **row["booking"]}
