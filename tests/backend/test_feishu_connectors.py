import json
from contextlib import contextmanager
from types import SimpleNamespace

from lockbot.backend.app.feishu import connectors


def test_sdk_pipe_acknowledges_only_after_event_acceptance(monkeypatch):
    app = SimpleNamespace(id=1, app_id="cli_test", app_secret="encrypted", transport="websocket")
    event = {"header": {"app_id": "cli_test"}}
    writes = []
    order = []

    class Output:
        ready = False

        def readline(self, limit):
            if not self.ready:
                self.ready = True
                return b'{"status":"connected"}\n'
            return (json.dumps({"event": event}) + "\n").encode()

        def close(self):
            pass

    class Input:
        def write(self, data):
            writes.append(json.loads(data))
            if "ack" in writes[-1]:
                order.append("ack")

        def flush(self):
            pass

        def close(self):
            pass

    class Process:
        stdin = Input()
        stdout = Output()

        def poll(self):
            return 0

        def wait(self, timeout):
            return 0

    class DB:
        def get(self, cls, identifier):
            return app

    @contextmanager
    def factory():
        yield DB()

    connector = connectors.Connector(1, factory=factory)
    monkeypatch.setattr(connectors.encryption, "decrypt", lambda value: "secret")
    monkeypatch.setattr(connectors.subprocess, "Popen", lambda *args, **kwargs: Process())

    def accept(db, selected, data):
        assert selected is app and data == event
        order.append("durable_claim")
        connector._stop.set()

    monkeypatch.setattr("lockbot.backend.app.feishu.service.accept_sdk_event", accept)
    connector._run()
    assert order == ["durable_claim", "ack"]
    assert writes[0] == {"app_id": "cli_test", "app_secret": "secret"}
    assert writes[1] == {"ack": True}


def test_manager_reuses_one_connection_per_application(monkeypatch):
    counts = []

    class FakeConnector:
        def __init__(self, identifier, factory=None):
            self.identifier = identifier
            self.thread = SimpleNamespace(is_alive=lambda: True)

        def start(self):
            counts.append(("start", self.identifier))

        def stop(self):
            counts.append(("stop", self.identifier))

    monkeypatch.setattr(connectors, "Connector", FakeConnector)
    monkeypatch.setattr(connectors.importlib.util, "find_spec", lambda name: True)
    manager = connectors.ConnectorManager()
    manager.connect(1)
    manager.connect(1)
    manager.connect(2)
    manager.stop()
    assert counts == [("start", 1), ("start", 2), ("stop", 1), ("stop", 2)]


def test_pinned_sdk_event_model_roundtrips_through_private_pipe(monkeypatch):
    import io
    import sys
    import time

    import pytest

    lark = pytest.importorskip("lark_oapi")
    from lockbot.backend.app.feishu.ws_process import main

    event = {
        "schema": "2.0",
        "header": {
            "app_id": "cli_test",
            "tenant_key": "tenant",
            "event_id": "test-event",
            "event_type": "im.message.receive_v1",
            "create_time": str(int(time.time() * 1000)),
        },
        "event": {
            "sender": {"sender_type": "user", "tenant_key": "tenant", "sender_id": {"open_id": "ou_alice"}},
            "message": {
                "message_id": "om_test",
                "create_time": str(int(time.time() * 1000)),
                "chat_type": "group",
                "chat_id": "oc_group",
                "message_type": "text",
                "content": '{"text":"query"}',
            },
        },
    }
    channel = io.StringIO()
    config = json.dumps({"app_id": "cli_test", "app_secret": "test-only-secret"})
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO((config + '\n{"ack":true}\n').encode())))
    monkeypatch.setattr(sys, "stdout", channel)
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    class Client:
        def __init__(self, app_id, app_secret, event_handler, log_level):
            self.handler = event_handler

        def start(self):
            self.handler.do_without_validation(json.dumps(event).encode())

    monkeypatch.setattr(lark.ws, "Client", Client)
    main()
    frame = json.loads(channel.getvalue())
    assert frame["event"]["header"]["app_id"] == "cli_test"
    assert frame["event"]["event"]["message"]["chat_id"] == "oc_group"
    assert "test-only-secret" not in channel.getvalue()
