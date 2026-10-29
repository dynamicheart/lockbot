"""Bounded application-level SDK subprocess lifecycle; resource bots own no connection."""

import importlib.util
import json
import logging
import subprocess
import sys
import threading
from contextlib import suppress

from lockbot.backend.app.bots import encryption
from lockbot.backend.app.database import SessionLocal
from lockbot.backend.app.feishu.models import FeishuApplication

logger = logging.getLogger(__name__)


class Connector:
    def __init__(self, application_id, factory=SessionLocal):
        self.application_id = application_id
        self.factory = factory
        self._stop = threading.Event()
        self._process = None
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name=f"feishu-app-{application_id}", daemon=True)

    def start(self):
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
        backoff = 2
        while not self._stop.is_set():
            process = None
            try:
                with self.factory() as db:
                    app = db.get(FeishuApplication, self.application_id)
                    if app is None or app.transport != "websocket":
                        return
                    config = {"app_id": app.app_id, "app_secret": encryption.decrypt(app.app_secret)}
                with self._lock:
                    if self._stop.is_set():
                        return
                    process = self._process = subprocess.Popen(
                        [sys.executable, "-m", "lockbot.backend.app.feishu.ws_process"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
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
            except Exception:
                logger.warning("Feishu application %s connector stopped; retrying", self.application_id)
            finally:
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
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60)


class ConnectorManager:
    def __init__(self):
        self._connectors = {}
        self._lock = threading.Lock()

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
