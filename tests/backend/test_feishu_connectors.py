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
        def readline(self, limit):
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
