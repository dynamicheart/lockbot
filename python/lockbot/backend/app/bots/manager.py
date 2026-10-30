"""
Bot instance manager (in-process, shared port).

All bots run inside the FastAPI process as BotInstance objects.
Webhook callbacks arrive at /api/bots/webhook/{bot_id} and are
dispatched to the corresponding BotInstance.

Admission and draining
----------------------

Every operation that touches a live instance takes a **lease** (``admit``). A lease is a
counted obligation, not a lock: it is held across the whole operation, including the time
spent blocked on the bot's own state lock and any best-effort notification afterwards. That
is what makes the bound cover waiting work as well as executing work.

Lifecycle transitions mark the bot *retiring* first, so new work is refused immediately,
then wait for outstanding leases to drain before the instance is replaced or dropped.

Ordering rules this module obeys (see the AC-7 analysis):

- ``BotManager._lock`` protects the registry **and** the admission bookkeeping. It is never
  held while acquiring a bot's state lock, while calling into a bot, or while waiting for a
  drain — ``Condition.wait`` releases it.
- The existing ``bot._lock`` remains the only lock protecting bot state. Nothing here adds
  a second serialization domain over state; this module serializes *metadata* only.
- The scheduler lock is taken only after this lock has been released, preserving the
  pre-existing ``bot._lock -> scheduler._lock`` direction.
- Draining never happens on the scheduler thread for a bot whose own sweep holds the lease,
  because that lease cannot complete until the callback returns.
"""

import logging
import os
import threading
import time
from contextlib import contextmanager

from lockbot.backend.app.resource_auth.service import handle_chat_binding
from lockbot.core.bot_instance import BotInstance
from lockbot.core.platforms.registry import normalize_platform_name
from lockbot.core.scheduler import BotScheduler

logger = logging.getLogger(__name__)

#: Outstanding operations allowed per bot, counting both queued and executing work. The
#: real contention point is the single non-reentrant bot state lock, so this is small on
#: purpose: a deeper queue would only grow latency, not throughput.
DEFAULT_MAX_INFLIGHT = int(os.environ.get("LOCKBOT_MAX_INFLIGHT_PER_BOT", 8))

#: How long a lifecycle transition waits for in-flight work before giving up.
DEFAULT_DRAIN_TIMEOUT = float(os.environ.get("LOCKBOT_DRAIN_TIMEOUT", 30.0))


class BotNotRunning(RuntimeError):
    """No live instance for this bot."""


class InstanceRetiring(RuntimeError):
    """The instance is being stopped, restarted or restored; new work is refused."""


class AdmissionFull(RuntimeError):
    """The per-bot bound on outstanding operations is reached. Shed, do not queue."""


class MayHaveExecuted(RuntimeError):
    """A lifecycle event overlapped an admitted operation.

    Deliberately *not* a rollback: the operation may already have mutated and persisted
    state. The only honest recovery is to read authoritative state.
    """

    def __init__(self, bot_id: int):
        self.bot_id = bot_id
        super().__init__(
            f"Operation on bot {bot_id} may have executed; inspect /api/bots/{bot_id}/resources "
            f"to see what is actually true. This is not a rollback."
        )


class Lease:
    """Proof that an operation was admitted against one specific live instance."""

    __slots__ = ("bot_id", "generation", "instance", "health")

    def __init__(self, bot_id: int, instance: BotInstance, generation: int, health: str | None = None):
        self.bot_id = bot_id
        self.instance = instance
        self.generation = generation
        # Authoritative health of the admitted generation: "error" for a retained failed
        # generation, otherwise None (the caller falls back to the DB row status).
        self.health = health

    @property
    def bot(self):
        return self.instance.bot


class BotManager:
    """
    Manages bot instances running in the FastAPI process.

    Usage:
        manager = BotManager()
        manager.start_scheduler()
        manager.start_bot(bot_id=1, config_dict={...})
        instance = manager.get_instance(bot_id=1)
        manager.stop_bot(bot_id=1)
        manager.shutdown_all()
    """

    def __init__(self, max_inflight: int = DEFAULT_MAX_INFLIGHT, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT):
        self._bots: dict[int, BotInstance] = {}
        self._lock = threading.Lock()
        self._drained = threading.Condition(self._lock)
        self._scheduler = BotScheduler()
        # Admission bookkeeping, all guarded by self._lock.
        self._retiring: set[int] = set()
        self._inflight: dict[int, int] = {}
        self._generations: dict[int, int] = {}
        # Generations that failed their sweep budget. Retained read-only (instance kept) so
        # /resources can still report health=error with the last state, until an operator
        # stops/restarts/deletes the bot. New writes and sweeps are refused; reads are allowed.
        self._failed: dict[int, BotInstance] = {}
        # Authoritative per-bot health, owned by the manager and captured with the read lease, so
        # a failed generation reports error even if the fatal DB status write failed. Only
        # 'error' is tracked here; a live/absent bot falls back to the DB row status.
        self._health: dict[int, str] = {}
        # Per-bot lifecycle transition owner. ONE reservation serializes every lifecycle
        # transition for a bot — start, stop, full restart, delete, and offline (stopped)
        # state/language edits — so the bad interleavings are unrepresentable rather than
        # individually patched. It is acquired under self._lock BEFORE any config read,
        # construction, or offline-edit validation and held through the route-level DB status +
        # audit commit. A competing transition on another thread is refused up front
        # (InstanceRetiring) with no durable effect. It is reentrant *per thread* so a route can
        # hold it while the manager methods it calls re-enter it, and ``restart`` can span both
        # its stop and start halves under one owner. The value is ``[owner_thread_id, depth]``.
        # DB status is never an ownership predicate; ownership lives here.
        self._transitions: dict[int, list] = {}
        # Exact-generation fatal-completion obligations, guarded by self._lock. A fatal sweep
        # registers one when it retains a failed generation and keeps it outstanding through the
        # off-lock SQL commit/rollback/close, decrementing in ``finally``. A stop drains it (so a
        # replacement cannot publish while a fatal write for the prior generation is still in
        # flight), and the fatal callback itself never drains.
        self._fatal_pending: dict[int, int] = {}
        # Generations that are *staged*: constructed and published to ``_bots`` but not yet
        # activated (no scheduler registration, admission refused). A staged generation has no
        # sweep and therefore no fatal completion, so a caller can commit its initial status
        # before activating it. ``bot_id -> generation``.
        self._staged: dict[int, int] = {}
        # Called (outside the lock) with (bot_id, generation) when a generation is finally
        # retired, so generation-keyed caches (snapshot cache, later notification/traffic state)
        # can evict. Registered by consumers at import time.
        self._retire_hooks: list = []
        self._max_inflight = max_inflight
        self._drain_timeout = drain_timeout
        self._migration_fences: dict[int, str] = {}
        self._migration_owners: dict[tuple[int, int], str] = {}

    def register_retire_hook(self, hook) -> None:
        """Register a ``hook(bot_id, generation)`` invoked at final generation retirement."""
        self._retire_hooks.append(hook)

    def _fire_retire_hooks(self, bot_id: int, generation: int) -> None:
        for hook in self._retire_hooks:
            try:
                hook(bot_id, generation)
            except Exception:
                logger.exception("retire hook failed for bot %d gen %d", bot_id, generation)

    def start_scheduler(self) -> None:
        """Start the shared scheduler background thread. Call once at app startup."""
        self._scheduler.start()

    # ------------------------------------------------------- transition owner

    def migration_fenced(self, bot_id: int) -> bool:
        with self._lock:
            return bot_id in self._migration_fences

    def migration_operation_id(self, bot_id: int) -> str | None:
        with self._lock:
            return self._migration_fences.get(bot_id)

    def install_migration_fence(self, bot_id: int, operation_id: str):
        with self._lock:
            previous = self._migration_fences.get(bot_id)
            if previous is not None and previous != operation_id:
                raise InstanceRetiring("Another migration owns this bot")
            self._migration_fences[bot_id] = operation_id

    def clear_migration_fence(self, bot_id: int, operation_id: str):
        with self._lock:
            if self._migration_fences.get(bot_id) != operation_id:
                raise InstanceRetiring("Migration fence does not match")
            del self._migration_fences[bot_id]

    def restore_migration_fences(self, records):
        """Startup only: rebuild durable admission fences before any bot autostarts."""
        with self._lock:
            if self._bots or self._failed or self._transitions:
                raise RuntimeError("Cannot replace migration fences while bots are live")
            self._migration_fences = dict(records)
            self._migration_owners.clear()

    @contextmanager
    def migration_operation(self, bot_id: int, operation_id: str):
        """Internal lifecycle capability; never bypasses resource-write admission."""
        key = threading.get_ident(), bot_id
        with self._lock:
            if self._migration_fences.get(bot_id) != operation_id:
                raise InstanceRetiring("Migration operation does not own this fence")
            if key in self._migration_owners:
                raise InstanceRetiring("Nested migration capability is not supported")
            self._migration_owners[key] = operation_id
        try:
            yield
        finally:
            with self._lock:
                self._migration_owners.pop(key, None)

    @contextmanager
    def transition(self, bot_id: int):
        """Acquire the per-bot lifecycle transition owner, or refuse up front.

        This is the single serialization point for every lifecycle transition. The first
        caller on a thread acquires it; a competing transition on a *different* thread is
        refused immediately with :class:`InstanceRetiring` and no durable effect. It is
        reentrant on the owning thread, so a route may hold it across the manager calls it
        makes and across its own DB status + audit commit, and ``restart`` may span its stop
        and start halves under one owner.

        Only ``self._lock`` is taken (briefly) to read/update the owner table; the owner is
        held across construction, draining and SQL without holding ``self._lock``.
        """
        me = threading.get_ident()
        with self._lock:
            owner = self._transitions.get(bot_id)
            fence = self._migration_fences.get(bot_id)
            if fence and self._migration_owners.get((me, bot_id)) != fence:
                raise InstanceRetiring(f"Bot {bot_id} is frozen for migration")
            if owner is not None and owner[0] != me:
                raise InstanceRetiring(f"Bot {bot_id} has a lifecycle transition in progress")
            if owner is None:
                self._transitions[bot_id] = [me, 1]
            else:
                owner[1] += 1
        try:
            yield
        finally:
            with self._lock:
                owner = self._transitions.get(bot_id)
                if owner is not None and owner[0] == me:
                    owner[1] -= 1
                    if owner[1] <= 0:
                        del self._transitions[bot_id]

    def is_running(self, bot_id: int) -> bool:
        return bot_id in self._bots

    def get_pid(self, bot_id: int) -> int | None:
        if bot_id in self._bots:
            return os.getpid()
        return None

    def get_instance(self, bot_id: int) -> BotInstance | None:
        """Get the BotInstance for webhook dispatching.

        Prefer :meth:`admit` for anything that mutates or reads bot state: a raw instance
        reference can outlive its replacement, and nothing stops a lifecycle transition
        from happening between this lookup and the work that follows it.
        """
        return self._bots.get(bot_id)

    # ---------------------------------------------------------------- admission

    @contextmanager
    def admit(
        self,
        bot_id: int,
        enforce_cap: bool = True,
        for_read: bool = False,
        expected_generation: int | None = None,
        expected_instance=None,
    ):
        """Admit one operation against the current live instance, or refuse it.

        The lease is released only when this context manager exits, which must be after the
        operation has finished entirely. A disconnected HTTP client therefore cannot free
        the slot early, and a drain cannot complete while the work is still running.

        ``enforce_cap=False`` reserves capacity for the bot's own expiry sweep: it still marks
        the work in flight (so a drain waits for it) and still refuses a retiring/stopped bot,
        but it is never shed for fullness, so API load cannot starve expiry notifications.

        ``expected_generation`` / ``expected_instance`` bind this admission to one registration:
        the managed scheduler sweep passes the generation and instance it was registered with, so
        if a stop+start publishes a replacement during the admission window the sweep is refused
        atomically (under this lock) rather than admitting and sweeping the replacement.

        ``for_read=True`` also admits a **read** against a failed generation (one that exhausted
        its sweep budget and is retained read-only): the lease carries ``health="error"`` and is
        counted so a drain waits for it, letting ``/resources`` report ``health=error`` with the
        last state. A write (``for_read=False``) against a failed generation is refused with
        ``BotNotRunning``. Failed reads still honour ``_max_inflight`` (a finite bound), so error
        polling cannot create unbounded contention on the single state lock.
        """
        with self._lock:
            if not for_read and bot_id in self._migration_fences:
                raise InstanceRetiring(f"Bot {bot_id} is frozen for migration")
            if bot_id in self._retiring:
                raise InstanceRetiring(f"Bot {bot_id} is retiring")
            instance = self._bots.get(bot_id)
            health = None
            if instance is None:
                if for_read and bot_id in self._failed:
                    instance = self._failed[bot_id]
                    health = self._health.get(bot_id, "error")
                else:
                    raise BotNotRunning(f"Bot {bot_id} is not running")
            else:
                # A constructed-but-not-yet-activated (staged) generation is not serving work
                # yet: refuse transiently rather than admit against it.
                if self._staged.get(bot_id) == self._generations.get(bot_id, 0):
                    raise InstanceRetiring(f"Bot {bot_id} is starting")
                # Authoritative health of the live generation, owned by the manager, so a reader
                # does not fall back to a possibly-stale DB status row.
                health = self._health.get(bot_id)
            # Refuse if this admission is bound to a registration that is no longer current.
            if expected_generation is not None and self._generations.get(bot_id, 0) != expected_generation:
                raise InstanceRetiring(f"Bot {bot_id} generation is no longer {expected_generation}")
            if expected_instance is not None and instance is not expected_instance:
                raise InstanceRetiring(f"Bot {bot_id} instance was replaced")
            current = self._inflight.get(bot_id, 0)
            # Every admitted operation (live or failed-read) honours the cap unless the caller
            # reserved capacity (enforce_cap=False, used only by the bot's own expiry sweep).
            if enforce_cap and current >= self._max_inflight:
                raise AdmissionFull(f"Bot {bot_id} has {current} operations outstanding")
            self._inflight[bot_id] = current + 1
            generation = self._generations.get(bot_id, 0)

        try:
            yield Lease(bot_id, instance, generation, health=health)
        finally:
            self._release_obligation(self._inflight, bot_id)

    def _release_obligation(self, counter: dict[int, int], bot_id: int) -> None:
        """Decrement a counted drain obligation under ``self._lock`` and wake any drainers.

        Shared by the admission lease (``_inflight``) and the fatal-completion obligation
        (``_fatal_pending``): both are counted, bot-keyed obligations that the stop-time
        wait (`_await_drained_locked`) blocks on, released identically.
        """
        with self._lock:
            remaining = counter.get(bot_id, 1) - 1
            if remaining <= 0:
                counter.pop(bot_id, None)
            else:
                counter[bot_id] = remaining
            self._drained.notify_all()

    def inflight(self, bot_id: int) -> int:
        with self._lock:
            return self._inflight.get(bot_id, 0)

    def is_retiring(self, bot_id: int) -> bool:
        with self._lock:
            return bot_id in self._retiring

    def is_failed(self, bot_id: int) -> bool:
        """True if a fatally-failed generation is retained read-only for this bot."""
        with self._lock:
            return bot_id in self._failed

    def generation(self, bot_id: int) -> int:
        with self._lock:
            return self._generations.get(bot_id, 0)

    def lease_is_current(self, lease: Lease) -> bool:
        """Defensive post-check: did the instance get replaced under this operation?"""
        with self._lock:
            return self._generations.get(lease.bot_id, 0) == lease.generation and (
                self._bots.get(lease.bot_id) is lease.instance
            )

    def _begin_retiring(self, bot_id: int) -> None:
        with self._lock:
            self._retiring.add(bot_id)

    def _end_retiring(self, bot_id: int) -> None:
        with self._lock:
            self._retiring.discard(bot_id)

    def _await_drained_locked(self, bot_id: int, deadline: float) -> None:
        """Wait until no admitted work or fatal completion is outstanding. Caller holds ``_lock``.

        Used as the first half of the atomic wait-and-retire in :meth:`stop_bot`: the wait and
        the subsequent instance removal happen under one uninterrupted ``_lock`` hold (the
        ``Condition.wait`` only releases ``_lock`` while actually blocked), so a fatal callback
        cannot register an obligation in a gap between "drain returned" and "instance removed".
        On timeout the bot stays retiring and nothing is replaced: a worker that may still
        mutate and persist state makes replacement unsafe, so reporting uncertainty is correct.
        """
        while self._inflight.get(bot_id, 0) > 0 or self._fatal_pending.get(bot_id, 0) > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MayHaveExecuted(bot_id)
            self._drained.wait(remaining)

    def start_bot(self, bot_id: int, config_dict: dict, *, activate: bool = True) -> int:
        """
        Start a bot instance in-process.

        With ``activate=True`` (manager-only callers) the generation is staged and immediately
        activated. With ``activate=False`` the generation is only **staged** — constructed and
        published, but with no scheduler registration and admission refused — so the caller can
        commit the initial ``running`` status/audit *before* calling :meth:`activate_bot`. Because
        a staged generation has no sweep, no fatal completion can run before that initial commit.
        The caller must either activate the generation or clean it up (``stop_bot``) if the commit
        fails; both must happen under the same transition owner.

        Returns:
            PID of the current process.

        Raises:
            RuntimeError: If the bot is already running.
            InstanceRetiring: If a previous generation is still retiring/draining, a failed
                generation is retained, or another lifecycle transition is in progress.
        """
        # Own the lifecycle transition for the whole start, and check eligibility UNDER the
        # lock BEFORE any construction. Construction loads state from disk and can migrate
        # files, persist max-duration clamps and send clamp notifications, so a refused start
        # must never reach it. Owning the transition also prevents a concurrent stop / delete /
        # offline edit from interleaving with an unpublished start.
        with self.transition(bot_id):
            with self._lock:
                if bot_id in self._bots:
                    raise RuntimeError(f"Bot {bot_id} is already running")
                if bot_id in self._retiring:
                    raise InstanceRetiring(f"Bot {bot_id} is retiring; cannot start a replacement yet")
                if bot_id in self._failed:
                    raise InstanceRetiring(f"Bot {bot_id} has a failed generation; stop it before restarting")
                if bot_id in self._fatal_pending:
                    raise InstanceRetiring(f"Bot {bot_id} has a fatal completion in flight; stop it first")

            # Construct OUTSIDE the registry lock (holding the transition owner), so this bot's
            # I/O does not block admission or other bots' lifecycle work.
            instance = BotInstance(
                config_dict.get("BOT_TYPE", "NODE"),
                config_dict,
                scheduler=self._scheduler,  # managed mode: shared scheduler
            )
            instance.bot.binding_hook = self._make_binding_hook(bot_id, config_dict)
            from lockbot.backend.app.resource_api.lease_history import LeaseJournal

            try:
                with instance.bot._lock:
                    instance.config._lifecycle_observer = LeaseJournal(bot_id, instance.bot)
            except Exception:
                logger.warning("Bot %s lifecycle evidence is unavailable", bot_id)

            with self._lock:
                # The transition owner guarantees no competing start published in the meantime.
                self._bots[bot_id] = instance
                # A fresh generation, so any lease taken against a previous instance is
                # detectable as stale even if the row id is reused. Generations are strictly
                # monotonic per bot (never reset on stop), which the scheduler relies on below.
                generation = self._generations.get(bot_id, 0) + 1
                self._generations[bot_id] = generation
                # Authoritative health for this live generation; a read lease carries it so
                # /resources does not depend on the separately-written DB status row.
                self._health[bot_id] = "running"
                # Stage it: admission is refused and no sweep runs until activate_bot, so the
                # caller can commit the initial status before any fatal completion is possible.
                self._staged[bot_id] = generation

            if activate:
                self._activate_generation(bot_id)
            logger.info("Started bot %d in-process (type=%s, staged=%s)", bot_id, instance.bot_type, not activate)
            return os.getpid()

    def activate_bot(self, bot_id: int) -> None:
        """Activate a staged generation: register its sweep and allow admission.

        Must be called by the start caller under the same transition owner that staged it, and
        only after the initial ``running`` status/audit has been committed. It activates whatever
        generation is currently staged (there can be only one, because the owner prevents any
        other start from publishing in between) and is a no-op if the generation was already
        activated or retired.
        """
        self._activate_generation(bot_id)

    def _activate_generation(self, bot_id: int) -> None:
        with self._lock:
            instance = self._bots.get(bot_id)
            generation = self._generations.get(bot_id, 0)
            if instance is None or self._staged.get(bot_id) != generation:
                return  # nothing staged for the current generation (already active or retired)

        # Register the sweep FIRST, while the generation is still staged (admission still refused).
        # Only after a successful registration do we clear the staged flag to open admission. If
        # ``scheduler.add`` raises, the generation stays staged — admission remains refused and no
        # orphaned admitted-but-unscheduled generation exists — and the exception propagates to the
        # caller, whose cleanup retires it (B26).
        self._scheduler.add(
            bot_id,
            instance,
            delay=0.0,
            on_fatal_error=self._make_fatal_error_handler(bot_id, generation),
            admission=lambda bid, g=generation, inst=instance: self.admit(
                bid, enforce_cap=False, expected_generation=g, expected_instance=inst
            ),
            generation=generation,
        )
        # Wire up state-change callback so new locks wake the scheduler immediately
        instance.bot._on_state_changed = lambda: self._scheduler.reschedule_soon(bot_id)
        with self._lock:
            if self._staged.get(bot_id) == generation:
                del self._staged[bot_id]
        # Admission is now open. Nudge the scheduler so a first sweep that happened to be
        # admission-refused during the staged→active window (between add and this clear) is retried
        # promptly rather than waiting out the idle interval.
        self._scheduler.reschedule_soon(bot_id)

    def _make_binding_hook(self, bot_id: int, config_dict: dict):
        """Build the identity-binding hook for a bot.

        core/ recognizes bind / unbind / bindings but owns no database, so the backend supplies
        the implementation here. The hook's authorization scope (which bot a binding is written
        against, and on which platform) is this manager's own immutable bot_id and the platform
        resolved at start, never the mutable config the dispatcher reads from. The dispatcher
        still passes the adapter's platform; we only use it to assert it agrees with ours.
        """
        authoritative_bot_id = bot_id
        authoritative_revision = config_dict.get("_IDENTITY_REVISION")
        authoritative_platform = normalize_platform_name(config_dict.get("PLATFORM"))

        def binding_hook(*, platform, external_user_id, bot_id, action, argument):
            if normalize_platform_name(platform) != authoritative_platform or bot_id != authoritative_bot_id:
                logger.warning(
                    "Refusing binding for bot %d: claimed identity (%r, platform %r) "
                    "disagrees with authoritative (%r, platform %r)",
                    authoritative_bot_id,
                    bot_id,
                    platform,
                    authoritative_bot_id,
                    authoritative_platform,
                )
                raise RuntimeError("binding identity mismatch")
            return handle_chat_binding(
                platform=authoritative_platform,
                external_user_id=external_user_id,
                bot_id=authoritative_bot_id,
                action=action,
                argument=argument,
                expected_revision=authoritative_revision,
            )

        return binding_hook

    def _make_fatal_error_handler(self, bot_id: int, generation: int | None = None):
        """Return a closure that retains the failed generation and marks it error in the DB.

        Bound to the exact ``generation`` that registered it (defaulting to the current
        generation at creation time): a stale callback whose generation has since been replaced
        by a restart is a no-op, so it cannot move a replacement into ``_failed`` or overwrite
        its DB status.
        """
        expected_generation = generation if generation is not None else self._generations.get(bot_id, 0)

        def _on_fatal_error(failed_bot_id: int) -> None:
            # Runs on the scheduler thread, AFTER the failing sweep's lease has been released
            # (see scheduler._run_check's finally) and AFTER the scheduler removed the
            # registration. Retain the instance read-only in _failed so /resources can still
            # report health=error with its last state. Do NOT drain here (that would block the
            # single scheduler thread) and do NOT mark retiring (retiring means a transition is
            # in progress; a failed generation is a stable read-only state until an operator
            # stops/restarts/deletes the bot). Already-admitted ordinary leases finish against
            # the same retained object.
            with self._lock:
                if self._generations.get(failed_bot_id, 0) != expected_generation:
                    # A restart has already published a newer generation; this is a stale
                    # callback for a retired generation. Do nothing (neither registry nor DB).
                    return
                if self._staged.get(failed_bot_id) == expected_generation:
                    # The generation is staged (not yet activated), so its sweep has not run and
                    # this cannot be a real fatal completion. Crucially, the caller has not yet
                    # committed the initial status, so we must never commit ``error`` ahead of it.
                    return
                instance = self._bots.pop(failed_bot_id, None)
                if instance is None:
                    # The generation was already retired (e.g. stopped) under the same number;
                    # there is nothing to fail. Do not touch the DB for an absent instance.
                    return
                self._failed[failed_bot_id] = instance
                # Authoritative health lives here, captured by the read lease, so error
                # health does not depend on the DB write below succeeding.
                self._health[failed_bot_id] = "error"
                # Register a counted fatal-completion obligation for this exact generation BEFORE
                # releasing the lock. It is kept outstanding through the whole off-lock SQL
                # sequence below and released in ``finally``. A concurrent stop drains it (so a
                # replacement cannot publish while this write is in flight); this callback never
                # drains, so the single scheduler thread is never blocked here.
                self._fatal_pending[failed_bot_id] = self._fatal_pending.get(failed_bot_id, 0) + 1

            # Update DB status — runs in scheduler thread, create own session
            try:
                from lockbot.backend.app.bots.models import Bot as BotModel
                from lockbot.backend.app.database import SessionLocal

                db = SessionLocal()
                try:
                    bot = db.query(BotModel).filter(BotModel.id == failed_bot_id).first()
                    # Defence-in-depth ownership check before the commit. The atomic
                    # wait-and-retire in stop_bot is the primary guarantee: a stop cannot
                    # finalize while this obligation is outstanding, and a callback that arrives
                    # after retirement found no instance above and already returned. This clause
                    # (generation still current AND still in ``_failed``) additionally covers a
                    # shutdown_all clear and keeps us from committing ``error`` for anything we no
                    # longer own. It is NOT the fix for the drain/finalize race; the atomic
                    # retirement is.
                    with self._lock:
                        still_ours = (
                            self._generations.get(failed_bot_id, 0) == expected_generation
                            and failed_bot_id in self._failed
                        )
                    if bot and still_ours:
                        bot.status = "error"
                        bot.pid = None
                        db.commit()
                        logger.critical(
                            "Bot %d (%s): marked as error in DB after repeated failures",
                            failed_bot_id,
                            bot.name,
                        )
                finally:
                    db.close()
            except Exception:
                logger.exception("Bot %d: failed to update DB status to error", failed_bot_id)
            finally:
                # Release the fatal-completion obligation only after the SQL session is fully
                # closed, so a draining stop cannot finalize until this write is truly done.
                self._release_obligation(self._fatal_pending, failed_bot_id)

        return _on_fatal_error

    def stop_bot(self, bot_id: int, drain_timeout: float | None = None) -> bool:
        """
        Stop a bot instance, draining admitted work first.

        Owns the lifecycle transition for the whole stop (so a concurrent start / delete /
        edit on another thread is refused), then: mark retiring (so nothing new is admitted),
        cancel scheduling, wait for outstanding leases *and any in-flight fatal completion*,
        and only then drop the instance.

        Returns:
            True if the stop ran.

        Raises:
            InstanceRetiring: If another lifecycle transition on a different thread owns this
                bot. The caller should treat the bot as still transitioning and retry.
            RuntimeError: If the bot is not running.
            MayHaveExecuted: If admitted work (or a fatal completion) did not finish in time.
                The bot stays retiring and is not replaced, because a worker that may still
                persist state makes replacement unsafe.
        """
        with self.transition(bot_id):
            with self._lock:
                in_live = bot_id in self._bots
                in_failed = bot_id in self._failed
                if not in_live and not in_failed:
                    raise BotNotRunning(f"Bot {bot_id} is not running")
                self._retiring.add(bot_id)
                generation = self._generations.get(bot_id, 0)

            # The scheduler remove must happen with ``_lock`` released: the scheduler has its own
            # lock and must be taken after ours. The remove is scoped to this generation: if a
            # concurrent stop+start already published a replacement, a delayed remove here must
            # not unschedule it.
            with self._scheduler.remove_expecting(generation):
                self._scheduler.remove(bot_id)

            # Atomic wait-and-retire: wait for outstanding work AND remove the instance under one
            # uninterrupted ``_lock`` hold, so a fatal callback cannot register an obligation in a
            # gap between the wait returning and the removal. A callback that registers first makes
            # this wait block; a callback that arrives after removal finds no owned instance (its
            # ``_bots``/``_failed`` pop yields nothing) and does nothing.
            fire_hook = False
            deadline = time.monotonic() + (self._drain_timeout if drain_timeout is None else drain_timeout)
            with self._lock:
                try:
                    self._await_drained_locked(bot_id, deadline)
                except MayHaveExecuted:
                    logger.error("Bot %d: drain timed out; instance left retiring and not replaced", bot_id)
                    raise
                # Finalize only if this transition's generation is still current. A concurrent
                # stop+start may have already retired our generation and published a replacement;
                # we must not pop the replacement or erase its admission accounting.
                if self._generations.get(bot_id, 0) == generation:
                    self._bots.pop(bot_id, None)
                    self._failed.pop(bot_id, None)
                    self._inflight.pop(bot_id, None)
                    self._health.pop(bot_id, None)
                    self._staged.pop(bot_id, None)
                    self._retiring.discard(bot_id)
                    fire_hook = True
            # Final retirement: let generation-keyed caches evict (outside the lock).
            if fire_hook:
                self._fire_retire_hooks(bot_id, generation)
            logger.info("Stopped bot %d", bot_id)
            return True

    def restart_bot(self, bot_id: int, config_dict: dict) -> int:
        """Stop then start a bot, both halves under ONE transition owner. Returns PID.

        Holding a single transition owner across the whole restart makes it one atomic
        lifecycle operation: no competing stop / delete / edit / start can interleave between
        the stop and start halves, and a delayed stop from a prior transition cannot overwrite
        this restart's result. The stop half drains, so an operation admitted before the
        restart completes before the replacement instance is published. A failed generation is
        also stopped first (and evicted) so restart is the natural one-step recovery from
        ``health=error``.
        """
        with self.transition(bot_id):
            with self._lock:
                needs_stop = bot_id in self._bots or bot_id in self._failed
            if needs_stop:
                self.stop_bot(bot_id)
            return self.start_bot(bot_id, config_dict)

    def shutdown_all(self) -> None:
        """Stop the scheduler and clear all running bots. Called on platform shutdown.

        Order matters: stop scheduler dispatch FIRST (no new sweeps), then best-effort drain each
        live generation so an in-flight admitted operation is given a bounded chance to finish
        before the registry is cleared, and only then clear. The drain is best-effort and bounded
        (a hung worker must not block process shutdown), so any remaining state is still cleared
        below. Fuller worker-compartment draining is a later stage.
        """
        import contextlib

        self._scheduler.stop()
        with self._lock:
            live_ids = list(self._bots.keys()) + [bid for bid in self._failed if bid not in self._bots]
        for bot_id in live_ids:
            with contextlib.suppress(Exception):
                with self._lock:
                    operation = self._migration_fences.get(bot_id)
                scope = self.migration_operation(bot_id, operation) if operation else contextlib.nullcontext()
                with scope:
                    self.stop_bot(bot_id, drain_timeout=1.0)
        with self._lock:
            self._bots.clear()
            self._failed.clear()
            self._inflight.clear()
            self._retiring.clear()
            self._health.clear()
            self._transitions.clear()
            self._fatal_pending.clear()
            self._staged.clear()
        # Clear scheduler state so it can be safely restarted (e.g. in tests)
        with self._scheduler._lock:
            self._scheduler._instances.clear()
            self._scheduler._heap.clear()
            self._scheduler._gens.clear()
            self._scheduler._failure_counts.clear()
            self._scheduler._callbacks.clear()
            self._scheduler._admissions.clear()
            self._scheduler._mgr_gens.clear()


# Module-level singleton
bot_manager = BotManager()
