"""Directory equality is advisory and never supplies dashboard authorization."""

import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lockbot.backend.app.personal_panel.router import create_panel

ORIGIN = "https://panel.example.test"
DIRECTORY = "synthetic-directory"


class Provider:
    def __init__(self):
        self.bindings = {
            key: {
                "issuer": "synthetic-issuer",
                "platform": platform,
                "realm": realm,
                "subject": subject,
                "scope": "scope-" + key,
                "ref": "binding-" + key,
            }
            for key, platform, realm, subject in (
                ("a", "platform-a", "realm-a", "im-a"),
                ("b", "platform-b", "realm-b", "im-b"),
                ("c", "platform-c", "realm-c", "im-c"),
            )
        }

    def authenticate(self, key):
        return self.bindings.get(key)

    def validate(self, grant):
        return grant in self.bindings.values()

    def report(self, grant):
        return [{"label": grant["scope"], "status": "ok", "allocations": []}]


class Resolver:
    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls = []

    def resolve(self, principal):
        self.calls.append(dict(principal))
        reply = self.replies.get(principal["subject"], match())
        if isinstance(reply, Exception):
            raise reply
        return reply


def match(**overrides):
    return {"directory": DIRECTORY, "subject": "private-person-id", "expires_at": time.time() + 60} | overrides


def build(tmp_path, resolver=None):
    provider = Provider()
    parent = FastAPI()
    parent.mount(
        "/me",
        create_panel(
            ORIGIN,
            tmp_path / "sessions.db",
            provider,
            identity_resolver=resolver,
            identity_directory=DIRECTORY if resolver else None,
        ),
    )
    return provider, TestClient(parent, base_url=ORIGIN), TestClient(parent, base_url=ORIGIN)


def login(agent, browser, key, mode="replace"):
    link = agent.post("/me/links", headers={"Authorization": "Bearer " + key})
    assert link.status_code == 200
    ticket = parse_qs(urlsplit(link.json()["login_url"]).fragment.split("?", 1)[1])["ticket"][0]
    response = browser.post("/me/login", headers={"Origin": ORIGIN}, json={"ticket": ticket, "mode": mode})
    assert response.status_code == 200


def test_cross_platform_authoritative_equality_never_adds_scopes(tmp_path):
    resolver = Resolver()
    provider, agent, browser = build(tmp_path, resolver)
    login(agent, browser, "a")
    assert browser.get("/me/session").json()["same_person_groups"] == []
    assert resolver.calls == []
    # A directory match cannot locate or attach another IM binding.
    assert [g["label"] for g in browser.get("/me/resources").json()["groups"]] == ["scope-a"]
    login(agent, browser, "b", "append")
    response = browser.get("/me/session")
    data = response.json()
    assert data["same_person_groups"] == [[0, 1]]
    assert len(data["identities"]) == 2
    assert "private-person-id" not in response.text and DIRECTORY not in response.text
    assert all(set(principal) == {"issuer", "platform", "realm", "subject"} for principal in resolver.calls)
    assert {g["label"] for g in browser.get("/me/resources").json()["groups"]} == {"scope-a", "scope-b"}
    assert len(resolver.calls) == 2  # Resource authorization never invokes the directory.
    provider.bindings.pop("b")
    assert browser.get("/me/session").json()["same_person_groups"] == []
    assert [g["label"] for g in browser.get("/me/resources").json()["groups"]] == ["scope-a"]


@pytest.mark.parametrize(
    "reply",
    [
        None,
        [],
        [match(), match()],
        [match(subject="one"), match(subject="two")],
        "private-person-id",
        {},
        match(expires_at=0),
        match(expires_at=float("nan")),
        match(expires_at=float("inf")),
        match(expires_at=True),
        match(expires_at="future"),
        match(expires_at=10**1000),
        match(subject=""),
        match(subject=" "),
        match(subject=[]),
        match(directory="foreign-directory"),
        match(extra="profile"),
        ValueError("private-directory-error"),
    ],
)
def test_unknown_or_ambiguous_results_do_not_claim_same_person(tmp_path, reply):
    resolver = Resolver({"im-b": reply})
    _, agent, browser = build(tmp_path, resolver)
    login(agent, browser, "a")
    login(agent, browser, "b", "append")
    response = browser.get("/me/session")
    assert response.status_code == 200
    assert response.json()["same_person_groups"] == []
    assert "private-directory-error" not in response.text
    assert len(response.json()["identities"]) == 2


def test_no_cache_and_distinct_directory_people_remain_separate(tmp_path):
    resolver = Resolver({"im-c": match(subject="other-private-person")})
    _, agent, browser = build(tmp_path, resolver)
    for key, mode in (("a", "replace"), ("b", "append"), ("c", "append")):
        login(agent, browser, key, mode)
    assert browser.get("/me/session").json()["same_person_groups"] == [[0, 1]]
    resolver.replies["im-b"] = None
    assert browser.get("/me/session").json()["same_person_groups"] == []
    assert len(resolver.calls) == 6


def test_resolver_mutation_cannot_change_identity_or_access(tmp_path):
    class MutatingResolver(Resolver):
        def resolve(self, principal):
            principal.update(subject="forged", scope="unbound-scope", ref="unbound-ref")
            return match()

    provider, agent, browser = build(tmp_path, MutatingResolver())
    original = json.loads(json.dumps(provider.bindings))
    login(agent, browser, "a")
    login(agent, browser, "b", "append")
    session = browser.get("/me/session").json()
    assert session["same_person_groups"] == [[0, 1]]
    assert {identity["subject"] for identity in session["identities"]} == {"im-a", "im-b"}
    assert provider.bindings == original
    assert {g["label"] for g in browser.get("/me/resources").json()["groups"]} == {"scope-a", "scope-b"}


def test_reused_mutable_reply_cannot_conflate_different_people(tmp_path):
    class ReusingResolver(Resolver):
        result = match()

        def resolve(self, principal):
            self.result["subject"] = principal["subject"]
            return self.result

    _, agent, browser = build(tmp_path, ReusingResolver())
    login(agent, browser, "a")
    login(agent, browser, "b", "append")
    assert browser.get("/me/session").json()["same_person_groups"] == []


def test_resolver_does_not_retain_revoked_identity_during_lookup(tmp_path):
    resolver = Resolver()
    provider, agent, browser = build(tmp_path, resolver)
    login(agent, browser, "a")
    login(agent, browser, "b", "append")
    resolve = resolver.resolve

    def revoke(principal):
        provider.bindings.pop("a", None)
        return resolve(principal)

    resolver.resolve = revoke
    assert browser.get("/me/session").status_code == 403


def test_resolver_is_off_by_default(tmp_path):
    _, agent, browser = build(tmp_path)
    login(agent, browser, "a")
    login(agent, browser, "b", "append")
    assert browser.get("/me/session").json()["same_person_groups"] == []


def test_multiple_scopes_of_one_im_principal_do_not_resolve_people(tmp_path):
    resolver = Resolver()
    provider, agent, browser = build(tmp_path, resolver)
    provider.bindings["b"] = dict(provider.bindings["a"], scope="scope-b", ref="binding-b")
    login(agent, browser, "a")
    login(agent, browser, "b", "append")
    session = browser.get("/me/session").json()
    assert session["same_person_groups"] == []
    assert len(session["identities"]) == 1
    assert set(session["identities"][0]["scopes"]) == {"scope-a", "scope-b"}
    assert resolver.calls == []


def test_setup_loads_only_explicit_local_resolver_and_pins_directory(tmp_path, monkeypatch):
    from lockbot.backend.app.personal_panel.setup import mount_if_configured

    plugin = tmp_path / "resolver.py"
    plugin.write_text("""
class Resolver:
    def __init__(self, config):
        self.config = config
    def resolve(self, principal):
        return None
def create_resolver(config):
    return Resolver(config)
""")
    config = {
        "origin": ORIGIN,
        "state_file": str(tmp_path / "state.db"),
        "provider": {"issuer": "example"},
        "identity_resolver": {"file": str(plugin), "config": {"directory": DIRECTORY}},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("LOCKBOT_PERSONAL_PANEL_CONFIG", str(path))
    parent = FastAPI()
    mount_if_configured(parent)
    with TestClient(parent, base_url=ORIGIN) as client:
        assert client.get("/api/personal-panel/session").json()["same_person_groups"] == []
    for directory in (None, "", " ", 17):
        config["identity_resolver"]["config"]["directory"] = directory
        path.write_text(json.dumps(config))
        with pytest.raises(ValueError, match="pinned directory"):
            mount_if_configured(FastAPI())


def test_direct_embedding_also_requires_directory_pin(tmp_path):
    with pytest.raises(ValueError, match="pinned directory"):
        create_panel(ORIGIN, tmp_path / "unused.db", Provider(), identity_resolver=Resolver())
