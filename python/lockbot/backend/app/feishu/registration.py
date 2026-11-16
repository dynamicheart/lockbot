"""Own short-lived Feishu registration sessions inside the single Lockbot process."""

import json
import re
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from secrets import token_urlsafe

from fastapi import HTTPException

from lockbot.backend.app.bots import encryption
from lockbot.backend.app.feishu.registration_process import REGISTRATION_TIMEOUT, confirmation_url
from lockbot.core.platforms.feishu_proxy import sdk_proxy_override

_PENDING = {"starting", "waiting", "confirmed", "importing"}
_WORKER_TERMINAL = {"denied", "expired", "failed"}
_MAX_REGISTRATIONS = 8


@dataclass
class Registration:
    owner: tuple[int, int]
    name: str
    status: str = "starting"
    deadline: float = field(default_factory=lambda: time.monotonic() + REGISTRATION_TIMEOUT)
    url: str | None = None
    url_deadline: float = 0
    app_id: str | None = None
    encrypted_secret: str | None = field(default=None, repr=False)
    application: dict | None = None
    process: subprocess.Popen | None = field(default=None, repr=False)
    timer: threading.Timer | None = field(default=None, repr=False)


def _stop_worker(process):
    if process is not None:
        with suppress(ProcessLookupError):
            process.kill()


class Registrations:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[str, Registration] = {}

    def start(self, owner, name):
        with self._lock:
            for identifier, entry in list(self._entries.items()):
                if entry.owner != owner:
                    continue
                current = self._snapshot(identifier, entry)
                if current["status"] in _PENDING and time.monotonic() < entry.deadline:
                    return current
                self._remove(identifier)
            if len(self._entries) >= _MAX_REGISTRATIONS:
                raise HTTPException(429, "Try again after a pending Feishu registration expires")
            identifier = token_urlsafe(24)
            entry = self._entries[identifier] = Registration(owner, name)
            entry.timer = threading.Timer(REGISTRATION_TIMEOUT, self._expire, args=(identifier,))
            entry.timer.daemon = True
            entry.timer.start()
            thread = threading.Thread(target=self._run, args=(identifier, entry), daemon=True)
            thread.start()
            return self._snapshot(identifier, entry)

    def status(self, identifier, owner):
        with self._lock:
            return self._snapshot(identifier, self._owned(identifier, owner))

    def cancel(self, identifier, owner):
        with self._lock:
            entry = self._owned(identifier, owner)
            if entry.status in {"importing", "imported"}:
                raise HTTPException(409, "Application import has already started")
            self._remove(identifier)

    def begin_import(self, identifier, owner):
        with self._lock:
            entry = self._owned(identifier, owner)
            if entry.status == "imported":
                return None, entry.application
            if entry.status != "confirmed":
                raise HTTPException(409, "Feishu registration is not ready to import")
            secret = encryption.decrypt(entry.encrypted_secret)
            entry.status = "importing"
            return (entry.name, entry.app_id, secret), None

    def finish_import(self, identifier, owner, *, application=None, retry=False):
        with self._lock:
            entry = self._entries.get(identifier)
            if entry is None or entry.owner != owner or entry.status != "importing":
                return
            if retry and time.monotonic() < entry.deadline:
                entry.status = "confirmed"
                return
            entry.encrypted_secret = None
            entry.status = "imported" if application is not None else "failed"
            entry.application = application

    def close(self):
        with self._lock:
            for identifier in list(self._entries):
                self._remove(identifier)

    def _owned(self, identifier, owner):
        entry = self._entries.get(identifier)
        if entry is None or entry.owner != owner or time.monotonic() >= entry.deadline:
            raise HTTPException(404, "Feishu registration not found or expired")
        return entry

    @staticmethod
    def _snapshot(identifier, entry):
        result = {"id": identifier, "status": entry.status, "name": entry.name}
        if entry.status == "waiting":
            if time.monotonic() >= entry.url_deadline:
                return {**result, "status": "expired"}
            return {**result, "url": entry.url, "expires_in": max(0, int(entry.url_deadline - time.monotonic()))}
        if entry.status == "confirmed":
            result["app_id"] = entry.app_id
        if entry.status == "imported":
            result["application"] = entry.application
        return result

    def _expire(self, identifier):
        with self._lock:
            self._remove(identifier)

    def _remove(self, identifier):
        if (entry := self._entries.pop(identifier, None)) is not None:
            entry.timer.cancel()
            entry.encrypted_secret = None
            _stop_worker(entry.process)

    def _receive(self, identifier, entry, message):
        with self._lock:
            if self._entries.get(identifier) is not entry or time.monotonic() >= entry.deadline:
                return False
            status = message["status"]
            if status == "waiting" and entry.status == "starting":
                if type(message["expires_in"]) is not int or not 0 < message["expires_in"] <= REGISTRATION_TIMEOUT:
                    raise ValueError("Invalid registration expiry")
                entry.url = confirmation_url(message["url"])
                entry.url_deadline = min(entry.deadline, time.monotonic() + message["expires_in"])
                entry.status = status
                return True
            if status == "confirmed" and entry.status == "waiting" and time.monotonic() < entry.url_deadline:
                app_id, secret = message["app_id"], message["app_secret"]
                if (
                    not isinstance(app_id, str)
                    or not re.fullmatch(r"cli_[A-Za-z0-9]{1,124}", app_id)
                    or not isinstance(secret, str)
                    or not secret.strip()
                    or len(secret) > 512
                ):
                    raise ValueError("Invalid registration credentials")
                entry.app_id = app_id
                entry.encrypted_secret = encryption.encrypt(secret)
            elif status not in _WORKER_TERMINAL:
                raise ValueError("Unexpected registration state")
            entry.url = None
            entry.status = status
            return False

    def _run(self, identifier, entry):
        process = None
        try:
            with self._lock:
                if self._entries.get(identifier) is not entry:
                    return
                process = entry.process = subprocess.Popen(
                    [sys.executable, "-m", "lockbot.backend.app.feishu.registration_process"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            config = {"name": entry.name, "proxy_url": sdk_proxy_override()}
            with process.stdin as pipe:
                pipe.write(json.dumps(config).encode() + b"\n")
            with process.stdout as pipe:
                while frame := pipe.readline(16384):
                    if not frame.endswith(b"\n") or not self._receive(identifier, entry, json.loads(frame)):
                        break
        except (OSError, TypeError, ValueError, KeyError):
            pass  # Never relay the SDK output or exception details to logs.
        finally:
            if process is not None:
                _stop_worker(process)
                process.wait()
            with self._lock:
                entry.process = None
                if entry.status in {"starting", "waiting"}:
                    entry.url = None
                    entry.status = "failed"


registrations = Registrations()
