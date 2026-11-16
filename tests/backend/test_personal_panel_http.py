"""The agent has no browser. Only the human browser receives a persistent session."""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lockbot.backend.app.personal_panel.router import COOKIE, create_panel

ORIGIN = "https://bot.example.test"


class Directory:
    def __init__(self):
        self.bindings = {
            "a-secret": dict(
                issuer="instance1", platform="feishu", realm="tenant1/app1", subject="same", scope="GPU A", ref="a"
            ),
            "b-secret": dict(
                issuer="instance1", platform="infoflow", realm="org1", subject="same", scope="GPU B", ref="b"
            ),
            "c-secret": dict(
                issuer="instance1", platform="feishu", realm="tenant2/app2", subject="same", scope="GPU C", ref="c"
            ),
        }

    def authenticate(self, key):
        return self.bindings.get(key)

    def validate(self, grant):
        return grant in self.bindings.values()

    def report(self, grant):
        return [dict(label=grant["scope"], status="ok", allocations=[dict(kind="hold", node=grant["ref"] + "-node")])]


@pytest.fixture
def env(tmp_path):
    provider = Directory()
    parent = FastAPI()
    parent.mount("/me", create_panel(ORIGIN, tmp_path / "state.db", provider))
    return provider, TestClient(parent, base_url=ORIGIN), TestClient(parent, base_url=ORIGIN)


def issue(agent, key):
    response = agent.post(
        "/me/links", headers={"Authorization": "Bearer " + key}, json={"platform": "forged", "subject": "admin"}
    )
    assert response.status_code == 200, response.text
    assert not agent.cookies and key not in response.text
    url = response.json()["login_url"]
    return parse_qs(urlsplit(url).fragment.split("?", 1)[1])["ticket"][0]


def post(browser, path, data):
    return browser.post("/me/" + path, json=data, headers={"Origin": ORIGIN})


def test_multi_platform_scope_and_revocation(env):
    directory, agent, browser = env
    ta = issue(agent, "a-secret")
    assert post(browser, "preview", {"ticket": ta}).json()["identity"]["platform"] == "feishu"
    assert browser.get("/me/session").json()["authenticated"] is False
    assert post(browser, "login", {"ticket": ta, "mode": "replace"}).status_code == 200
    old = browser.cookies.get(COOKIE)
    for key in ("b-secret", "c-secret"):
        t = issue(agent, key)
        assert post(browser, "login", {"ticket": t, "mode": "append"}).status_code == 200
    assert len(browser.get("/me/session").json()["identities"]) == 3
    agent.cookies.set(COOKIE, old)
    assert agent.get("/me/resources").status_code == 401
    directory.bindings.pop("a-secret")
    groups = browser.get("/me/resources").json()["groups"]
    assert {g["label"] for g in groups} == {"GPU B", "GPU C"}
    assert post(browser, "logout", {}).status_code == 200
    assert browser.get("/me/resources").status_code == 401


def test_origin_scope_and_replay(env):
    directory, agent, browser = env
    t = issue(agent, "a-secret")
    assert agent.post("/me/links", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert browser.get("/me/session", headers={"host": "evil.invalid"}).status_code == 403
    request = {"ticket": t, "mode": "replace"}
    assert browser.post("/me/login", json=request, headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert post(browser, "login", request).status_code == 200
    assert post(browser, "login", request).status_code == 410
    assert browser.post("/me/links", headers={"Origin": ORIGIN}).status_code == 401
    directory.bindings.clear()
    assert browser.get("/me/resources").status_code == 401


def test_same_verified_principal_has_two_separately_revocable_scopes(env):
    directory, agent, browser = env
    directory.bindings["d-secret"] = dict(directory.bindings["a-secret"], ref="d", scope="GPU D")
    for secret, mode in [("a-secret", "replace"), ("d-secret", "append")]:
        assert post(browser, "login", {"ticket": issue(agent, secret), "mode": mode}).status_code == 200
    identities = browser.get("/me/session").json()["identities"]
    assert len(identities) == 1
    assert set(identities[0]["scopes"]) == {"GPU A", "GPU D"}
    directory.bindings.pop("a-secret")
    assert browser.get("/me/session").json()["identities"][0]["scopes"] == ["GPU D"]
    assert [g["label"] for g in browser.get("/me/resources").json()["groups"]] == ["GPU D"]


def test_dashboard_is_opt_in_and_precedes_spa(tmp_path, monkeypatch):
    import json

    from lockbot.backend.app.main import create_app

    # A real fallback index makes this assertion independent of frontend build artifacts.
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<title>Fallback</title>")
    monkeypatch.setenv("FRONTEND_DIST", str(frontend))
    monkeypatch.delenv("LOCKBOT_PERSONAL_PANEL_CONFIG", raising=False)
    with TestClient(create_app(), base_url=ORIGIN) as client:
        assert not any(getattr(route, "path", None) == "/api/personal-panel" for route in client.app.routes)
    config = tmp_path / "panel.json"
    config.write_text(
        json.dumps({"origin": ORIGIN, "state_file": str(tmp_path / "sessions.db"), "provider": {"issuer": "example"}})
    )
    monkeypatch.setenv("LOCKBOT_PERSONAL_PANEL_CONFIG", str(config))
    with TestClient(create_app(), base_url=ORIGIN) as client:
        assert client.get("/api/personal-panel/capabilities").json()["home"] == ORIGIN + "/#/me"
        assert client.get("/api/personal-panel/session").json()["authenticated"] is False
        assert "Fallback" not in client.get("/me/").text
        assert (
            client.post("/api/personal-panel/links", headers={"Authorization": "Bearer not-a-binding"}).status_code
            == 401
        )


def test_cross_origin_cannot_read_cookie_scoped_data(env):
    _, agent, browser = env
    response = post(browser, "login", {"ticket": issue(agent, "a-secret"), "mode": "replace"})
    assert response.status_code == 200
    for endpoint in ("/me/session", "/me/resources"):
        assert browser.get(endpoint, headers={"Origin": "https://sibling.example.test"}).status_code == 403


def test_plugin_inventory_counts_mounted_reporters_without_exposing_their_metadata(tmp_path):
    class Reporter:
        label = "Test external resource source"

        def report(self, _grant):
            return []

    provider = Directory()
    parent = FastAPI()
    parent.mount("/me", create_panel(ORIGIN, tmp_path / "state.db", provider, [Reporter()]))
    agent = TestClient(parent, base_url=ORIGIN)
    browser = TestClient(parent, base_url=ORIGIN)
    assert "workbench_plugins" not in agent.get("/me/session").json()
    assert post(browser, "login", {"ticket": issue(agent, "a-secret"), "mode": "replace"}).status_code == 200
    response = browser.get("/me/session")
    assert response.json()["workbench_plugins"] == {"resource_reporters": 1}
    assert Reporter.label not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    provider.bindings.clear()
    assert "workbench_plugins" not in browser.get("/me/session").json()


def test_first_binding_revoked_while_second_group_loads(env):
    directory, agent, browser = env
    for key, mode in [("a-secret", "replace"), ("b-secret", "append")]:
        assert post(browser, "login", {"ticket": issue(agent, key), "mode": mode}).status_code == 200
    original = directory.report

    def revoke_other(grant):
        if grant["ref"] == "b":
            directory.bindings.pop("a-secret")
        return original(grant)

    directory.report = revoke_other
    result = browser.get("/me/resources")
    assert result.status_code == 403
    assert "a-node" not in result.text


@pytest.mark.parametrize("origin", ["http://10.1.2.3:8000", "http://127.0.0.1:8000", "http://[fd00::1]:8000"])
def test_explicit_private_http_cookie_and_logout(tmp_path, origin):
    parent = FastAPI()
    parent.mount("/me", create_panel(origin, tmp_path / "http.db", Directory(), allow_private_http=True))
    agent, browser = TestClient(parent, base_url=origin), TestClient(parent, base_url=origin)
    assert agent.get("/me/capabilities").json()["transport"] == "private-http"
    ticket = issue(agent, "a-secret")
    login = browser.post("/me/login", json={"ticket": ticket, "mode": "replace"}, headers={"Origin": origin})
    assert login.status_code == 200
    cookie = login.headers["set-cookie"]
    assert "lockbot_me_http=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Secure" not in cookie and "Path=/me" in cookie and "Domain=" not in cookie
    assert browser.get("/me/resources").status_code == 200
    assert browser.post("/me/logout", json={}, headers={"Origin": origin}).status_code == 200
    assert browser.get("/me/resources").status_code == 401


@pytest.mark.parametrize(
    "origin,allow", [("http://10.1.2.3", False), ("http://example.test", True), ("http://8.8.8.8", True)]
)
def test_http_requires_explicit_private_ip(tmp_path, origin, allow):
    with pytest.raises(ValueError):
        create_panel(origin, tmp_path / "rejected.db", Directory(), allow_private_http=allow)


def test_explicit_local_identity_provider_uses_same_browser_api(tmp_path, monkeypatch):
    import json

    from lockbot.backend.app.personal_panel.setup import mount_if_configured

    plugin = tmp_path / "provider.py"
    plugin.write_text("""
class Provider:
    def authenticate(self, key):
        if key != 'synthetic-secret':
            return None
        return dict(issuer='fixture', platform='test-im', realm='fixture',
                    subject='fixture-person', scope='fixture-group', ref='fixture-binding')
    def validate(self, grant):
        return grant == self.authenticate('synthetic-secret')
    def report(self, grant):
        return []
def create_provider(config):
    return Provider()
""")
    config = tmp_path / "provider.json"
    config.write_text(
        json.dumps(
            {
                "origin": ORIGIN,
                "state_file": str(tmp_path / "plugin.db"),
                "provider": {"file": str(plugin), "config": {}},
            }
        )
    )
    monkeypatch.setenv("LOCKBOT_PERSONAL_PANEL_CONFIG", str(config))
    parent = FastAPI()
    mount_if_configured(parent)
    with TestClient(parent, base_url=ORIGIN) as client:
        response = client.post("/api/personal-panel/links", headers={"Authorization": "Bearer synthetic-secret"})
        assert response.status_code == 200
        url = response.json()["login_url"]
        assert "synthetic-secret" not in url and urlsplit(url).fragment.startswith("/me?ticket=")
        assert client.post("/api/personal-panel/links", headers={"Authorization": "Bearer other"}).status_code == 401


def test_reports_identify_their_source_without_overriding_login_identity(tmp_path):
    class Reporter:
        label = "共享报表"

        def report(self, grant):
            return [dict(label="External group", status="ok", allocations=[], source={"kind": "forged"})]

    provider = Directory()
    parent = FastAPI()
    parent.mount("/me", create_panel(ORIGIN, tmp_path / "sources.db", provider, [Reporter()]))
    browser = TestClient(parent, base_url=ORIGIN)
    ticket = issue(TestClient(parent, base_url=ORIGIN), "b-secret")
    assert post(browser, "login", {"ticket": ticket, "mode": "replace"}).status_code == 200
    groups = browser.get("/me/resources").json()["groups"]
    assert [group["source"] for group in groups] == [
        {"kind": "identity_provider", "label": "登录服务"},
        {"kind": "reporter", "label": "共享报表"},
    ]
    assert groups[0]["identity"] == groups[1]["identity"]


def test_resources_stream_returns_only_authenticated_grants_and_matches_json(env):
    import json

    _, agent, browser = env
    assert agent.get("/me/resources-stream").status_code == 401
    for key, mode in [("a-secret", "replace"), ("b-secret", "append")]:
        assert post(browser, "login", {"ticket": issue(agent, key), "mode": mode}).status_code == 200
    streamed = browser.get("/me/resources-stream")
    assert streamed.headers["cache-control"] == "no-store"
    assert streamed.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in streamed.iter_lines()]
    assert events[0] == {"event": "start", "total": 2}
    assert events[-1] == {"event": "complete"}
    groups = [group for event in events[1:-1] for group in event["groups"]]
    assert sorted(groups, key=lambda group: group["label"]) == sorted(
        browser.get("/me/resources").json()["groups"], key=lambda group: group["label"]
    )
    assert {group["label"] for group in groups} == {"GPU A", "GPU B"}
    selectors = {
        value
        for group in groups
        for value in (
            group["detail_id"],
            group["allocations"][0]["detail_id"],
            group["allocations"][0]["node_detail_id"],
        )
    }
    assert len(selectors) == 6
    assert all(len(value) == 32 and value == f"{int(value, 16):032x}" for value in selectors)


def test_http_stream_delivers_fast_group_before_waiting_and_rechecks_revocation(env):
    import json
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    directory, agent, browser = env
    for key, mode in [("a-secret", "replace"), ("b-secret", "append")]:
        assert post(browser, "login", {"ticket": issue(agent, key), "mode": mode}).status_code == 200

    release_slow = threading.Event()
    original_report = directory.report

    def report(grant):
        if grant["ref"] == "b":
            assert release_slow.wait(8)
        return original_report(grant)

    directory.report = report
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(browser.app, lifespan="off", access_log=False, log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(50):
                if server.started:
                    break
                assert thread.is_alive()
                time.sleep(0.05)
            assert server.started
            headers = {"Host": urlsplit(ORIGIN).netloc, "Cookie": f"{COOKIE}={browser.cookies.get(COOKIE)}"}
            url = f"http://127.0.0.1:{port}/me/resources-stream"
            with httpx.Client(trust_env=False, timeout=5) as http, http.stream("GET", url, headers=headers) as stream:
                assert stream.status_code == 200
                events = (json.loads(line) for line in stream.iter_lines())
                assert next(events) == {"event": "start", "total": 2}
                first = next(events)
                assert first["event"] == "batch"
                assert [group["label"] for group in first["groups"]] == ["GPU A"]
                assert not release_slow.is_set()
                directory.bindings.pop("b-secret")
                release_slow.set()
                assert list(events) == [{"event": "unavailable"}]
        finally:
            release_slow.set()
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()


def test_third_party_reports_cannot_supply_owner_task_details(env):
    directory, agent, browser = env
    snapshot = directory.report

    def report(grant):
        groups = snapshot(grant)
        groups[0]["allocations"][0]["own_task"] = {"note": "untrusted aftermarket note"}
        groups[0]["utilization"] = {"configuration": {"endpoint": "https://untrusted.example.invalid"}}
        return groups

    directory.report = report
    assert post(browser, "login", {"ticket": issue(agent, "a-secret"), "mode": "replace"}).status_code == 200
    for path in ("resources", "resources-stream"):
        response = browser.get("/me/" + path)
        assert response.status_code == 200
        assert "own_task" not in response.text
        assert "untrusted aftermarket note" not in response.text
        assert "utilization" not in response.text


def test_reporter_samples_are_scoped_and_expire_in_both_browser_endpoints(env):
    directory, agent, browser = env
    snapshot = directory.report
    now = datetime.now(timezone.utc)
    deadline = (now + timedelta(seconds=45)).isoformat()

    def report(grant):
        groups = snapshot(grant)
        group = groups[0]
        group["source_at"] = now.isoformat()
        group["allocations"][0]["utilization_sample"] = {
            "compute_percent": 0,
            "memory_percent": None,
            "expires_at": deadline,
            "raw": "discard sample payload",
        }
        group["allocations"] += [
            {
                "kind": "reservation",
                "node": "queued",
                "utilization_sample": group["allocations"][0]["utilization_sample"],
            },
            {
                "kind": "hold",
                "node": "expired-sample",
                "utilization_sample": {"expires_at": (now - timedelta(seconds=1)).isoformat()},
            },
        ]
        return groups

    directory.report = report
    assert post(browser, "login", {"ticket": issue(agent, "a-secret"), "mode": "replace"}).status_code == 200
    for path in ("resources", "resources-stream"):
        response = browser.get("/me/" + path)
        assert response.status_code == 200
        assert response.text.count('"utilization_sample"') == 1
        assert '"compute_percent"' in response.text and '"memory_percent"' in response.text
        assert "discard sample payload" not in response.text
