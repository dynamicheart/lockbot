"""Bounded application-level SDK subprocess lifecycle; resource bots own no connection."""

import importlib.util
import json
import logging
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timezone

from lockbot.backend.app.database import SessionLocal
from lockbot.backend.app.feishu.models import FeishuApplication
from lockbot.backend.app.platform_applications.service import CredentialUnavailable, credential_expiry, credentials_for
from lockbot.core.platforms.feishu_proxy import sdk_proxy_override

logger = logging.getLogger(__name__)


class Connector:
    def __init__(self, application_id, factory=SessionLocal):
        self.application_id = application_id
        self.factory = factory
        self._stop = threading.Event()
        self._process = None
        self._lock = threading.Lock()
        self._state = "stopped"
        self._last_handshake_at = None
        self.thread = threading.Thread(target=self._run, name=f"feishu-app-{application_id}", daemon=True)

    def snapshot(self):
        """Local lifecycle observations only; no network calls or live-reachability claim."""
        with self._lock:
            state = self._state if self.thread.is_alive() else "stopped"
            return {"state": state, "last_handshake_at": self._last_handshake_at}

    def _set_state(self, state, *, handshake=False):
        with self._lock:
            self._state = state
            if handshake:
                self._last_handshake_at = datetime.now(timezone.utc).isoformat()

    def start(self):
        self._set_state("starting")
        self.thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            process = self._process
        if process:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    process.kill()
                process.wait(timeout=5)
        self.thread.join(timeout=10)

    def _run(self):
        try:
            self._run_loop()
        finally:
            with self._lock:
                self._state = "stopped"
                self._process = None

    def _run_loop(self):
        backoff = 2
        while not self._stop.is_set():
            self._set_state("starting")
            process = None
            expiry_timer = None
            try:
                with self.factory() as db:
                    app = db.get(FeishuApplication, self.application_id)
                    if app is None or app.transport != "websocket":
                        return
                    config = {"app_id": app.app_id, "app_secret": credentials_for(app)["app_secret"]}
                    deadline = credential_expiry(app)
                    override = sdk_proxy_override()
                    if override is not None:
                        config["proxy_url"] = override
                with self._lock:
                    if self._stop.is_set():
                        return
                    process = self._process = subprocess.Popen(
                        [sys.executable, "-m", "lockbot.backend.app.feishu.ws_process"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                if deadline is not None:
                    remaining = deadline.replace(tzinfo=timezone.utc).timestamp() - time.time()
                    if remaining <= 0:
                        raise CredentialUnavailable("Messaging credentials have expired")

                    def expire_receiver(process=process):
                        with suppress(ProcessLookupError):
                            process.terminate()

                    expiry_timer = threading.Timer(remaining, expire_receiver)
                    expiry_timer.daemon = True
                    expiry_timer.start()
                process.stdin.write((json.dumps(config) + "\n").encode())
                process.stdin.flush()
                while not self._stop.is_set():
                    line = process.stdout.readline(256 * 1024 + 1024)
                    if not line:
                        break
                    if len(line) > 256 * 1024 or not line.endswith(b"\n"):
                        raise ValueError("Invalid connector frame")
                    frame = json.loads(line)
                    if frame == {"status": "connected"}:
                        self._set_state("running", handshake=True)
                        backoff = 2
                        logger.info("Feishu application %s WebSocket handshake completed", self.application_id)
                        continue
                    data = frame["event"]
                    ack = False
                    try:
                        from lockbot.backend.app.feishu.service import accept_sdk_event

                        with self.factory() as db:
                            app = db.get(FeishuApplication, self.application_id)
                            if app and app.transport == "websocket":
                                accept_sdk_event(db, app, data)
                                ack = True
                    except Exception:
                        logger.warning("Feishu application %s did not accept an event", self.application_id)
                    process.stdin.write((json.dumps({"ack": ack}) + "\n").encode())
                    process.stdin.flush()
                    backoff = 2
            except CredentialUnavailable:
                logger.warning("Feishu application %s credentials unavailable; receiver stopped", self.application_id)
                return
            except Exception:
                logger.warning("Feishu application %s connector stopped; retrying", self.application_id)
            finally:
                if expiry_timer is not None:
                    expiry_timer.cancel()
                if process:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    process.stdin.close()
                    process.stdout.close()
                with self._lock:
                    self._process = None
                    self._state = "stopped" if self._stop.is_set() else "retrying"
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60)


class ConnectorManager:
    def __init__(self):
        self._connectors = {}
        self._lock = threading.RLock()

    def snapshot(self, application_id):
        with self._lock:
            connector = self._connectors.get(application_id)
            return connector.snapshot() if connector else {"state": "stopped", "last_handshake_at": None}

    @contextmanager
    def application_guard(self):
        """Serialize application removal and route creation in this single-instance service."""
        with self._lock:
            yield

    @contextmanager
    def application_change(self, application_id, *, before_stop=None, recover=None):
        """Serialize configuration changes against connector admission; stop only this app."""
        with self._lock:
            if before_stop:
                before_stop()
            connector = self._connectors.get(application_id)
            if connector:
                connector.stop()
                if connector.thread.is_alive():
                    raise RuntimeError("Application receiver has not stopped")
                self._connectors.pop(application_id, None)
            try:
                yield
            except BaseException:
                if connector is not None and recover is not None:
                    try:
                        recover()
                    except Exception:
                        logger.error("Could not recover Feishu application %s receiver", application_id)
                raise

    def connect(self, application_id, factory=SessionLocal):
        if importlib.util.find_spec("lark_oapi") is None:
            raise ValueError("Install lockbot[feishu] to use WebSocket receiving")
        with self._lock:
            if application_id in self._connectors:
                existing = self._connectors[application_id]
                if existing.thread.is_alive():
                    return
                self._connectors.pop(application_id)
            if len(self._connectors) >= 16:
                raise ValueError("At most 16 Feishu application connectors are supported")
            connector = Connector(application_id, factory=factory)
            connector.start()
            self._connectors[application_id] = connector

    def start(self, factory=SessionLocal):
        with factory() as db:
            apps = db.query(FeishuApplication).filter_by(transport="websocket").all()
            if apps and importlib.util.find_spec("lark_oapi") is None:
                logger.error("Feishu WebSocket requires the lockbot[feishu] optional dependency")
                return
            for app in apps:
                try:
                    self.connect(app.id, factory=factory)
                except Exception:
                    logger.error("Could not start Feishu application %s; other bots remain available", app.id)

    def reconfigure(self, factory=SessionLocal):
        # Hold admission until every old receiver has stopped; never run two for one app.
        with self._lock:
            for connector in self._connectors.values():
                connector.stop()
                if connector.thread.is_alive():
                    raise RuntimeError("A Feishu receiver has not stopped; restart required")
            self._connectors.clear()
            self.start(factory=factory)

    def stop(self):
        with self._lock:
            current = list(self._connectors.values())
            self._connectors.clear()
        for connector in current:
            try:
                connector.stop()
            except Exception:
                logger.error("Could not fully stop Feishu application %s connector", connector.application_id)


connectors = ConnectorManager()
