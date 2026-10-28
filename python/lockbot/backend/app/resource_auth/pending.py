"""Temporary store for unconfirmed bindings.

An unconfirmed binding is **never** written to the database: the binding-creation route is
unauthenticated, so a durable write there would be a table-flood vector. It is held here,
in process, until a signature-verified chat confirmation claims it.

Three consequences are accepted deliberately, and are documented rather than hidden:

1. The flood target moves from the database to memory, so the outstanding-record caps are
   mandatory, not advisory.
2. A restart before confirmation permanently invalidates the issued credential. The agent
   must create a new binding.
3. The design fails rather than degrades if more than one process ever serves one
   deployment, because a code created by one process cannot be confirmed by another.
   Single-process operation is enforced separately by a startup ownership lock on the bot
   state store. The store therefore sits behind this thin interface, so the dependency is
   explicit and a different backing store can be substituted.
"""

from __future__ import annotations

import secrets
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PendingCapExceeded(Exception):
    """Raised when an outstanding-unconfirmed cap would be exceeded."""

    def __init__(self, scope: str):
        self.scope = scope
        super().__init__(f"Too many outstanding unconfirmed bindings ({scope})")


class PendingCollision(Exception):
    """Raised when a generated binding id or code digest already exists.

    The store never overwrites an existing entry: a silent overwrite would orphan the first
    binding (its code would become unclaimable) or let one code claim a different binding.
    The caller regenerates and tries again instead.
    """


@dataclass(frozen=True)
class PendingBinding:
    """An issued-but-unconfirmed binding.

    Carries no plaintext secret: both the bind code and the credential are present only as
    digests, so dumping this store leaks neither.
    """

    binding_id: str
    code_digest: str
    credential_digest: str
    bind_expires_at: datetime
    created_at: datetime
    #: Per-source cap key (the creating client's address). Not an identity.
    source: str = ""
    #: Agent-declared label, carried through to the durable row for the chat listing.
    label: str | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or _utcnow()) >= self.bind_expires_at


class PendingBindingStore(ABC):
    """Thin interface over wherever unconfirmed bindings live."""

    @abstractmethod
    def create(
        self,
        *,
        binding_id: str,
        code_digest: str,
        credential_digest: str,
        source: str = "",
        label: str | None = None,
        window_seconds: int,
        max_total: int,
        max_per_source: int,
        now: datetime | None = None,
    ) -> PendingBinding:
        """Record a new unconfirmed binding, or raise :class:`PendingCapExceeded`."""

    @abstractmethod
    def claim(self, code_digest: str, *, now: datetime | None = None) -> PendingBinding | None:
        """Atomically remove and return the record for ``code_digest``.

        Returns None if there is no such record or its window has closed. The window is
        checked here, at claim time, so a pruning job that never runs cannot revive an
        expired code. Because the record is removed by the first successful claim, the
        claim is single-use and two concurrent confirmations produce exactly one success.

        This is the convenience form of :meth:`reserve` + :meth:`finalize` for callers with
        no durable write to protect; a caller that must persist first should reserve instead.
        """

    @abstractmethod
    def reserve(self, code_digest: str, *, now: datetime | None = None) -> tuple[PendingBinding, str] | None:
        """Mark the record for ``code_digest`` as in flight and hold it under a token.

        Returns ``(record, reservation_token)``, or None if there is no live record or it is
        already being confirmed. A reserved record stays an outstanding record: it keeps its
        global and per-source cap slot and keeps its id and code digest reserved against new
        issuance. It is only no longer *claimable* by another :meth:`reserve`/:meth:`claim`,
        so exactly one caller can be confirming it at a time. The caller must then either
        :meth:`finalize` the token (after its durable write commits) or :meth:`restore` it (if
        the durable write fails), so a failed confirmation does not strand the code.
        """

    @abstractmethod
    def finalize(self, token: str) -> None:
        """Drop a reserved record for good, freeing its cap slot. Called after the commit."""

    @abstractmethod
    def restore(self, token: str) -> None:
        """Make a reserved record claimable again, unchanged. Called when the write failed.

        The record kept its place the whole time, so its original deadline, source and cap
        occupancy are intact and it can never have been displaced by a colliding issuance. A
        no-op if the token is unknown (already finalized or restored).
        """

    @abstractmethod
    def get(self, binding_id: str, *, now: datetime | None = None) -> PendingBinding | None:
        """Return the unconfirmed record for ``binding_id``, pruning expired records."""

    @abstractmethod
    def purge_all(self) -> None:
        """Drop every unconfirmed record. Used on backup restore."""

    @abstractmethod
    def count(self, *, now: datetime | None = None) -> int:
        """Number of outstanding unconfirmed records (after pruning)."""


class InProcessPendingBindingStore(PendingBindingStore):
    """A dict plus a lock. Pruning happens on access; nothing schedules cleanup."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, PendingBinding] = {}
        self._by_code: dict[str, str] = {}  # code_digest -> binding_id
        # Records being confirmed right now. They stay in _by_id / _by_code (so they keep their
        # cap slot and reserve their id/code against new issuance) but are not claimable again.
        self._reserved: set[str] = set()  # binding_ids in flight
        self._tokens: dict[str, str] = {}  # reservation_token -> binding_id

    # -- internals (callers hold self._lock) ------------------------------------
    def _prune_locked(self, now: datetime) -> None:
        # An in-flight (reserved) record is never pruned out from under its confirmation; its
        # fate is decided by finalize()/restore().
        expired = [bid for bid, rec in self._by_id.items() if bid not in self._reserved and rec.is_expired(now)]
        for bid in expired:
            self._drop_locked(bid)

    def _drop_locked(self, binding_id: str) -> PendingBinding | None:
        record = self._by_id.pop(binding_id, None)
        if record is not None:
            self._by_code.pop(record.code_digest, None)
        return record

    # -- interface --------------------------------------------------------------
    def create(
        self,
        *,
        binding_id: str,
        code_digest: str,
        credential_digest: str,
        source: str = "",
        label: str | None = None,
        window_seconds: int,
        max_total: int,
        max_per_source: int,
        now: datetime | None = None,
    ) -> PendingBinding:
        now = now or _utcnow()
        record = PendingBinding(
            binding_id=binding_id,
            code_digest=code_digest,
            credential_digest=credential_digest,
            bind_expires_at=now + timedelta(seconds=window_seconds),
            created_at=now,
            source=source,
            label=label,
        )
        with self._lock:
            self._prune_locked(now)
            if len(self._by_id) >= max_total:
                raise PendingCapExceeded("global")
            if source and sum(1 for r in self._by_id.values() if r.source == source) >= max_per_source:
                raise PendingCapExceeded("per-source")
            if binding_id in self._by_id or code_digest in self._by_code:
                raise PendingCollision()
            self._by_id[binding_id] = record
            self._by_code[code_digest] = binding_id
        return record

    def claim(self, code_digest: str, *, now: datetime | None = None) -> PendingBinding | None:
        reservation = self.reserve(code_digest, now=now)
        if reservation is None:
            return None
        record, token = reservation
        self.finalize(token)
        return record

    def reserve(self, code_digest: str, *, now: datetime | None = None) -> tuple[PendingBinding, str] | None:
        now = now or _utcnow()
        with self._lock:
            self._prune_locked(now)
            binding_id = self._by_code.get(code_digest)
            if binding_id is None or binding_id in self._reserved:
                # No live record, or one already being confirmed by someone else.
                return None
            record = self._by_id.get(binding_id)
            if record is None or record.is_expired(now):
                return None
            # Mark in flight WITHOUT removing from _by_id / _by_code: the record keeps its cap
            # slot and keeps its id/code reserved against new issuance until finalize/restore.
            token = secrets.token_urlsafe(16)
            self._reserved.add(binding_id)
            self._tokens[token] = binding_id
            return record, token

    def finalize(self, token: str) -> None:
        with self._lock:
            binding_id = self._tokens.pop(token, None)
            if binding_id is None:
                return
            # The durable row committed; the record is no longer outstanding, so drop it and
            # free its cap slot and id/code reservation.
            self._reserved.discard(binding_id)
            self._drop_locked(binding_id)

    def restore(self, token: str) -> None:
        with self._lock:
            binding_id = self._tokens.pop(token, None)
            if binding_id is None:
                return
            # The durable write failed; the record never left _by_id / _by_code, so simply make
            # it claimable again with its original deadline, source and cap occupancy intact.
            # No collision is possible: new issuance was rejected while the id/code was reserved.
            self._reserved.discard(binding_id)

    def get(self, binding_id: str, *, now: datetime | None = None) -> PendingBinding | None:
        now = now or _utcnow()
        with self._lock:
            self._prune_locked(now)
            return self._by_id.get(binding_id)

    def purge_all(self) -> None:
        with self._lock:
            self._by_id.clear()
            self._by_code.clear()
            self._reserved.clear()
            self._tokens.clear()

    def count(self, *, now: datetime | None = None) -> int:
        with self._lock:
            self._prune_locked(now or _utcnow())
            return len(self._by_id)


#: Process-wide store. Single-process operation is a precondition of this design.
pending_store: PendingBindingStore = InProcessPendingBindingStore()
