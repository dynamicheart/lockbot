"""Persistent browser sessions containing independently proven IM bindings.

The caller verifies a binding before issuing a ticket and obtains explicit browser
consent before using ``mode='append'``. Appending proves control of both bindings;
it does not establish that their subjects are the same natural person. This module
stores references and identity metadata only, never native binding credentials.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

TICKET_LIFETIME = 120
SESSION_LIFETIME = 30 * 86400
MAX_GRANTS = 32
_GRANT_FIELDS = frozenset(("issuer", "platform", "realm", "subject", "scope", "ref"))


class PanelSessions:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Set permissions before SQLite can write any metadata.
        descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), timeout=30, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA secure_delete=ON")
        with self._transaction():
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS panel_tickets "
                "(digest TEXT PRIMARY KEY, grant_json TEXT NOT NULL, "
                "expires REAL NOT NULL)"
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS panel_tickets_expiry ON panel_tickets(expires)")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS panel_sessions "
                "(digest TEXT PRIMARY KEY, grants_json TEXT NOT NULL, "
                "expires REAL NOT NULL)"
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS panel_sessions_expiry ON panel_sessions(expires)")

    @contextmanager
    def _transaction(self):
        # BEGIN IMMEDIATE serializes read/consume/rotate across connections and
        # processes as well as the threads sharing this instance.
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    @staticmethod
    def _digest(token):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            return None
        try:
            return hashlib.sha256(token.encode("ascii")).hexdigest()
        except UnicodeEncodeError:
            return None

    @staticmethod
    def _grant_json(grant):
        if not isinstance(grant, dict) or set(grant) != _GRANT_FIELDS:
            raise ValueError("Invalid binding grant")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 2048 for value in grant.values()):
            raise ValueError("Invalid binding grant")
        return json.dumps(grant, sort_keys=True, ensure_ascii=True, separators=(",", ":"))

    def _prune(self, now):
        tickets = self.db.execute("DELETE FROM panel_tickets WHERE expires<=?", (now,)).rowcount
        sessions = self.db.execute("DELETE FROM panel_sessions WHERE expires<=?", (now,)).rowcount
        return {"tickets": tickets, "sessions": sessions}

    def issue(self, grant):
        """Issue a two-minute, single-use handoff for a verified binding."""
        encoded = self._grant_json(grant)
        ticket = secrets.token_urlsafe(32)
        with self._transaction():
            now = time.time()
            self._prune(now)
            self.db.execute(
                "INSERT INTO panel_tickets VALUES (?,?,?)", (self._digest(ticket), encoded, now + TICKET_LIFETIME)
            )
        return ticket

    def peek(self, ticket):
        """Inspect a ticket without logging in or consuming it (safe prefetch)."""
        digest = self._digest(ticket)
        with self.lock:
            row = self.db.execute(
                "SELECT grant_json FROM panel_tickets WHERE digest=? AND expires>?", (digest, time.time())
            ).fetchone()
        if row is None:
            raise ValueError("Invalid or expired handoff")
        return json.loads(row[0])

    def consume(self, ticket, current_cookie=None, mode="replace"):
        """Consume proof and rotate the cookie atomically.

        Replacement retains only the newly proven binding. Explicit append
        requires a live existing cookie and keeps grants separate. The HTTP
        caller must check CSRF/origin and obtain consent before selecting append.
        Failed requests leave their tickets available until expiry.
        """
        if mode not in ("replace", "append"):
            raise ValueError("Invalid session mode")
        digest = self._digest(ticket)
        current_digest = self._digest(current_cookie)
        cookie = secrets.token_urlsafe(32)
        with self._transaction():
            now = time.time()
            row = self.db.execute(
                "SELECT grant_json FROM panel_tickets WHERE digest=? AND expires>?", (digest, now)
            ).fetchone()
            if row is None:
                raise ValueError("Invalid or expired handoff")
            grant = json.loads(row[0])
            grants = []
            if mode == "append":
                existing = self.db.execute(
                    "SELECT grants_json FROM panel_sessions WHERE digest=? AND expires>?", (current_digest, now)
                ).fetchone()
                if existing is None:
                    raise ValueError("Append requires a current session")
                grants = json.loads(existing[0])
            # Only exact binding duplicates collapse; subjects are never merged
            # across issuer/platform/realm or inferred from matching strings.
            if grant not in grants:
                if len(grants) >= MAX_GRANTS:
                    raise ValueError("Session binding limit reached")
                grants.append(grant)
            self.db.execute("DELETE FROM panel_tickets WHERE digest=?", (digest,))
            self.db.execute("DELETE FROM panel_sessions WHERE digest=?", (current_digest,))
            self.db.execute(
                "INSERT INTO panel_sessions VALUES (?,?,?)",
                (self._digest(cookie), json.dumps(grants, ensure_ascii=True), now + SESSION_LIFETIME),
            )
            self._prune(now)
        return cookie

    def grant_window(self, cookie):
        """Return referenced bindings and the original browser session deadline."""
        with self._transaction():
            now = time.time()
            self._prune(now)
            row = self.db.execute(
                "SELECT grants_json, expires FROM panel_sessions WHERE digest=? AND expires>?",
                (self._digest(cookie), now),
            ).fetchone()
        return (json.loads(row[0]), row[1]) if row else ([], None)

    def grants(self, cookie):
        return self.grant_window(cookie)[0]

    def logout(self, cookie):
        """Revoke the browser bearer; repeated logout is harmless."""
        with self._transaction():
            self.db.execute("DELETE FROM panel_sessions WHERE digest=?", (self._digest(cookie),))
            self._prune(time.time())

    def prune(self):
        """Remove expired tickets and sessions, returning the removed counts."""
        with self._transaction():
            return self._prune(time.time())

    def remove_grant(self, grant, *, required_cookie=None):
        """Remove only the rotated binding, preserving other scopes and expiry."""
        with self._transaction():
            self._prune(time.time())
            authorized = True
            if required_cookie is not None:
                current = self.db.execute(
                    "SELECT grants_json FROM panel_sessions WHERE digest=? AND expires>?",
                    (self._digest(required_cookie), time.time()),
                ).fetchone()
                authorized = current is not None and grant in json.loads(current[0])
            for digest, encoded in self.db.execute("SELECT digest,grants_json FROM panel_sessions").fetchall():
                previous = json.loads(encoded)
                remaining = [item for item in previous if item != grant]
                if remaining == previous:
                    continue
                if remaining:
                    self.db.execute(
                        "UPDATE panel_sessions SET grants_json=? WHERE digest=?", (json.dumps(remaining), digest)
                    )
                else:
                    self.db.execute("DELETE FROM panel_sessions WHERE digest=?", (digest,))
            return authorized

    def close(self):
        with self.lock:
            self.db.close()
