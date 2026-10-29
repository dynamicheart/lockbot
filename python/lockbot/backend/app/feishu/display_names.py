"""Bounded, bot-scoped display-name enrichment; never a directory lookup service."""

import json
import os
import re
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from lockbot.backend.app.auth.dependencies import get_current_user
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import get_db
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
from lockbot.backend.app.feishu.service import application_client
from lockbot.backend.app.managed_paths import ManagedPathError, get_managed_paths
from lockbot.backend.app.rate_limit import limiter
from lockbot.backend.app.resource_api.privacy import public_log_entry
from lockbot.backend.app.resource_auth.models import ResourceBinding

router = APIRouter(prefix="/api/feishu/bots", tags=["Feishu"])
_ID = re.compile(r"ou_[A-Za-z0-9_-]{1,240}\Z")
_ACTOR = re.compile(r"^\[(ou_[A-Za-z0-9_-]{1,240})\]\s")
MAX_ACTORS = 32
LOG_READ_BYTES = 256 * 1024


class NameBatches:
    """Four workers, at most eight running/queued batches, one batch per app client."""

    def __init__(self, executor=None):
        self.lock = threading.Lock()
        self.active = {}
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="feishu-names")

    def submit(self, client, identities):
        missing = [uid for uid, state in client.user_name_states(identities).items() if state["status"] == "unknown"]
        if not missing:
            return
        key = client.app_id
        with self.lock:
            if key in self.active or len(self.active) >= 8:
                return
            self.active[key] = set(missing)
        try:
            self.executor.submit(self._run, key, client, missing)
        except RuntimeError:
            with self.lock:
                self.active.pop(key, None)

    def _run(self, key, client, identities):
        try:
            client.warm_user_names(identities)
        finally:
            with self.lock:
                self.active.pop(key, None)

    def pending(self, client):
        with self.lock:
            return set(self.active.get(client.app_id, ()))


batches = NameBatches()


def _scope(db, bot_id, user_id, token_version):
    """Fresh, authorized route snapshot. No caller-supplied actor identities."""
    db.rollback()
    user = db.get(User, user_id)
    if user is None or user.effective_token_version != token_version:
        raise HTTPException(401, "Authentication changed")
    bot = db.get(Bot, bot_id)
    if bot is None or bot.is_deleted or (bot.user_id != user.id and user.role not in {"admin", "super_admin"}):
        raise HTTPException(404, "Bot not found")
    if str(bot.platform).lower() != "feishu":
        raise HTTPException(409, "Display-name lookup is available for Feishu bots")
    route = db.query(FeishuGroupRoute).filter_by(bot_id=bot.id).first()
    app = db.get(FeishuApplication, route.application_id) if route else None
    if route is None or app is None or route.chat_id != bot.group_id:
        raise HTTPException(409, "Feishu route changed or is unavailable")
    signature = (
        bot.uuid,
        bot.identity_revision,
        bot.user_id,
        bot.group_id,
        route.id,
        route.chat_id,
        app.id,
        app.app_id,
        app.app_secret,
        app.tenant_key,
        app.bot_open_id,
        app.transport,
    )
    return bot, app, signature


def _log_actors(bot_id, page):
    """Only IDs reconstructed through the public log schema from a bounded current page."""
    try:
        path = get_managed_paths().bot_file(bot_id, "logs.jsonl")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return [], True
            start = max(0, info.st_size - LOG_READ_BYTES)
            source.seek(start)
            body = source.read(LOG_READ_BYTES)
        lines = body.splitlines()
        if start:
            lines = lines[1:]
        rows = []
        for line in reversed(lines[-1000:]):
            if len(line) > 8192:
                continue
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(row, dict):
                rows.append(public_log_entry(row))
        result = []
        for row in rows[(page - 1) * 50 : page * 50]:
            match = _ACTOR.match(row.get("message", ""))
            if match and row.get("category") == "command":
                result.append(match[1])
        return result, bool(start)
    except FileNotFoundError:
        return [], False
    except (OSError, ManagedPathError):
        return [], True


def _actors(db, bot, log_page):
    bindings = (
        db.query(ResourceBinding.external_user_id)
        .filter_by(
            bound_bot_id=bot.id,
            bound_bot_uuid=bot.uuid,
            bound_identity_revision=bot.identity_revision,
            platform="feishu",
        )
        .order_by(ResourceBinding.id.desc())
        .limit(MAX_ACTORS + 1)
        .all()
    )
    log_actors, log_incomplete = _log_actors(bot.id, log_page)
    values = [bot.last_user_id, *log_actors, *[row[0] for row in bindings]]
    values = list(dict.fromkeys(value for value in values if isinstance(value, str) and _ID.fullmatch(value)))
    return values[:MAX_ACTORS], len(values) > MAX_ACTORS or log_incomplete


def _read(bot_id, response, db, user, *, refresh, log_page):
    response.headers["Cache-Control"] = "no-store"
    user_id, version = user.id, user.effective_token_version
    bot, app, signature = _scope(db, bot_id, user_id, version)
    identities, truncated = _actors(db, bot, log_page)
    client = application_client(app)
    # No DB session, bot admission lease or state lock is passed to the network worker.
    db.rollback()
    if refresh:
        batches.submit(client, identities)
    # Read admission first: completion between these snapshots must not turn a ready
    # label into a false limited state that the UI would never poll again.
    pending = batches.pending(client)
    states = client.user_name_states(identities)
    names = {uid: state["name"] for uid, state in states.items() if state["status"] == "ready"}
    unknown = {uid for uid, state in states.items() if state["status"] == "unknown"}
    unavailable = sum(state["status"] in {"unavailable", "timeout", "permission"} for state in states.values())
    limited = bool(unknown - pending) or truncated
    status = "pending" if unknown & pending else "limited" if limited else "unavailable" if unavailable else "ready"
    # The route, ownership, token version or role may have changed while gathering labels.
    _, _, current = _scope(db, bot_id, user_id, version)
    if current != signature:
        raise HTTPException(409, "Bot or Feishu route changed; refresh the page")
    return {
        "names": names,
        "status": status,
        "pending_count": len(unknown & pending),
        "unavailable_count": unavailable,
        "truncated": truncated,
    }


class DisplayNameRefresh(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/{bot_id}/display-names")
@limiter.limit("20/minute")
def refresh_names(
    bot_id: int,
    request: Request,
    response: Response,
    log_page: int = Query(1, ge=1, le=20),
    body: DisplayNameRefresh | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _read(bot_id, response, db, user, refresh=True, log_page=log_page)


@router.get("/{bot_id}/display-names")
@limiter.limit("60/minute")
def cached_names(
    bot_id: int,
    request: Request,
    response: Response,
    log_page: int = Query(1, ge=1, le=20),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _read(bot_id, response, db, user, refresh=False, log_page=log_page)
