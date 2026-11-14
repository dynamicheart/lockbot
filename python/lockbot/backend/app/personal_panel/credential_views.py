"""Purpose-isolated, short-lived native credential disclosure proofs.

Only digests of random tickets, exact grants and opaque credential pins persist.
These tickets cannot log in, append identities, or authorize rotation.
"""

import json
import secrets
import time

from .sessions import TICKET_LIFETIME


class CredentialViews:
    def __init__(self, sessions):
        self.sessions = sessions
        with sessions._transaction():
            sessions.db.execute(
                "CREATE TABLE IF NOT EXISTS panel_credential_views "
                "(digest TEXT PRIMARY KEY, grant_json TEXT NOT NULL, pin TEXT NOT NULL, expires REAL NOT NULL)"
            )
            sessions.db.execute(
                "CREATE INDEX IF NOT EXISTS panel_credential_views_expiry ON panel_credential_views(expires)"
            )

    def issue(self, grant, pin, binding_expiry=None):
        encoded = self.sessions._grant_json(grant)
        ticket = secrets.token_urlsafe(32)
        with self.sessions._transaction():
            now = time.time()
            expiry = min(now + TICKET_LIFETIME, binding_expiry) if binding_expiry is not None else now + TICKET_LIFETIME
            if expiry <= now:
                raise ValueError("Binding expired")
            self.sessions.db.execute("DELETE FROM panel_credential_views WHERE expires<=?", (now,))
            self.sessions.db.execute(
                "INSERT INTO panel_credential_views VALUES (?,?,?,?)",
                (self.sessions._digest(ticket), encoded, pin, expiry),
            )
        return ticket, expiry

    def inspect(self, ticket, cookie, *, consume=False):
        """Require the exact currently authorized browser grant, atomically on use."""
        with self.sessions._transaction():
            now = time.time()
            self.sessions.db.execute("DELETE FROM panel_credential_views WHERE expires<=?", (now,))
            row = self.sessions.db.execute(
                "SELECT grant_json,pin,expires FROM panel_credential_views WHERE digest=? AND expires>?",
                (self.sessions._digest(ticket), now),
            ).fetchone()
            if row is None:
                raise ValueError("View link expired or used")
            session = self.sessions.db.execute(
                "SELECT grants_json FROM panel_sessions WHERE digest=? AND expires>?",
                (self.sessions._digest(cookie), now),
            ).fetchone()
            grant = json.loads(row[0])
            if session is None or grant not in json.loads(session[0]):
                raise PermissionError("Matching current browser authorization required")
            if consume:
                self.sessions.db.execute(
                    "DELETE FROM panel_credential_views WHERE digest=?", (self.sessions._digest(ticket),)
                )
            return grant, row[1], row[2]
