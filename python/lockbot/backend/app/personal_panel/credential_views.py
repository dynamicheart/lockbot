"""Purpose-isolated, short-lived native credential proofs.

Only digests of random tickets, exact grants and opaque credential pins persist.
Distinct tables prevent login, disclosure and rotation proof conversion. The
purpose is selected only by the server, never by request data.
"""

import json
import secrets
import time

from .sessions import TICKET_LIFETIME


class CredentialViews:
    def __init__(self, sessions, *, purpose="view_current_key"):
        self.sessions = sessions
        self.table = {
            "view_current_key": "panel_credential_views",
            "rotate_link": "panel_credential_rotation_links",
            "rotate_intent": "panel_credential_rotation_intents",
        }[purpose]
        with sessions._transaction():
            sessions.db.execute(
                f"CREATE TABLE IF NOT EXISTS {self.table} "
                "(digest TEXT PRIMARY KEY, grant_json TEXT NOT NULL, pin TEXT NOT NULL, expires REAL NOT NULL)"
            )
            sessions.db.execute(f"CREATE INDEX IF NOT EXISTS {self.table}_expiry ON {self.table}(expires)")

    def issue(self, grant, pin, binding_expiry=None):
        encoded = self.sessions._grant_json(grant)
        ticket = secrets.token_urlsafe(32)
        with self.sessions._transaction():
            now = time.time()
            expiry = min(now + TICKET_LIFETIME, binding_expiry) if binding_expiry is not None else now + TICKET_LIFETIME
            if expiry <= now:
                raise ValueError("Binding expired")
            self.sessions.db.execute(f"DELETE FROM {self.table} WHERE expires<=?", (now,))
            self.sessions.db.execute(
                f"INSERT INTO {self.table} VALUES (?,?,?,?)",
                (self.sessions._digest(ticket), encoded, pin, expiry),
            )
        return ticket, expiry

    def inspect(self, ticket, cookie, *, consume=False):
        """Require the exact currently authorized browser grant, atomically on use."""
        with self.sessions._transaction():
            now = time.time()
            self.sessions.db.execute(f"DELETE FROM {self.table} WHERE expires<=?", (now,))
            row = self.sessions.db.execute(
                f"SELECT grant_json,pin,expires FROM {self.table} WHERE digest=? AND expires>?",
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
                self.sessions.db.execute(f"DELETE FROM {self.table} WHERE digest=?", (self.sessions._digest(ticket),))
            return grant, row[1], row[2]
