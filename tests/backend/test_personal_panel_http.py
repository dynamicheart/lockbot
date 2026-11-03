"""The agent has no browser. Only the human browser receives a persistent session."""

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
    return parse_qs(urlsplit(url).fragment)["ticket"][0]


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
        assert not any(getattr(route, "path", None) == "/me" for route in client.app.routes)
    config = tmp_path / "panel.json"
    config.write_text(
        json.dumps({"origin": ORIGIN, "state_file": str(tmp_path / "sessions.db"), "provider": {"issuer": "example"}})
    )
    monkeypatch.setenv("LOCKBOT_PERSONAL_PANEL_CONFIG", str(config))
    with TestClient(create_app(), base_url=ORIGIN) as client:
        assert client.get("/me/capabilities").json()["home"] == ORIGIN + "/me/"
        assert client.get("/me/session").json()["authenticated"] is False
        assert "Fallback" not in client.get("/me/").text
        assert client.post("/me/links", headers={"Authorization": "Bearer not-a-binding"}).status_code == 401


def test_cross_origin_cannot_read_cookie_scoped_data(env):
    _, agent, browser = env
    response = post(browser, "login", {"ticket": issue(agent, "a-secret"), "mode": "replace"})
    assert response.status_code == 200
    for endpoint in ("/me/session", "/me/resources"):
        assert browser.get(endpoint, headers={"Origin": "https://sibling.example.test"}).status_code == 403


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
