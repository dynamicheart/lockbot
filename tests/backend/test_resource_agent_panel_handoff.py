"""The packaged CLI issues a native panel handoff and leaves login to the browser."""

# ruff: noqa: F811

import io
import json
import urllib.error
import zipfile
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lockbot.backend.app.personal_panel.native import NativeProvider
from lockbot.backend.app.personal_panel.router import create_panel
from lockbot.backend.app.resource_api.skill import skill_artifacts

from backend.test_resource_agent_helper import helper, write_private
from backend.test_resource_api_resources import api  # noqa: F401

BASE = "https://panel.example.test"
CAPABILITIES = "/api/personal-panel/capabilities"
ISSUE = "/api/personal-panel/links"
HOME = BASE + "/#/me"


class PanelOpener:
    def __init__(self, client):
        self.client = client
        self.requests = []
        self.capability_change = {}
        self.issue_change = {}
        self.issue_status = None

    def open(self, request, timeout):
        assert timeout > 0
        path = urlsplit(request.full_url).path
        self.requests.append((path, request.method, request.get_header("Authorization"), request.data))
        if path == ISSUE and self.issue_status is not None:
            raise urllib.error.HTTPError(request.full_url, self.issue_status, "", {}, None)
        response = self.client.request(
            request.method, request.full_url, headers=dict(request.header_items()), content=request.data
        )
        if response.is_error:
            raise urllib.error.HTTPError(request.full_url, response.status_code, "", response.headers, None)
        alterations = {CAPABILITIES: self.capability_change, ISSUE: self.issue_change}.get(path)
        data = json.dumps({**response.json(), **alterations}).encode() if alterations else response.content
        stream = io.BytesIO(data)
        stream.headers = response.headers
        return stream


@pytest.fixture
def handoff(client, api, session_factory, tmp_path, monkeypatch, request):
    base = getattr(request, "param", BASE)
    parent = FastAPI()
    provider = NativeProvider({"issuer": base, "session_factory": session_factory})
    parent.mount(
        "/api/personal-panel",
        create_panel(base, tmp_path / "panel.sqlite3", provider, allow_private_http=base.startswith("http://")),
    )
    parent.mount("/", client.app)
    agent = TestClient(parent, base_url=base, follow_redirects=False)
    browser = TestClient(parent, base_url=base)
    context = agent.get("/api/agent/context", headers=api["headers"]).json()
    headers = api["headers"]
    original = headers["Authorization"][7:]
    key = write_private(tmp_path / "resource.key", original)
    connection = write_private(
        tmp_path / "connection.json",
        {
            "approved_origin": base,
            "instance_id": context["server"]["instance_id"],
            "bot_uuid": context["bot"]["uuid"],
            "actor": context["actor"],
            "key_file": key.name,
        },
    )
    _, archive, meta = skill_artifacts()
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        zipped.extractall(tmp_path)
    opener = PanelOpener(agent)
    client_type = helper.Client

    def client_for_fixture(*args, **kwargs):
        return client_type(*args, root=tmp_path / meta["name"], opener=opener, **kwargs)

    monkeypatch.setattr(helper, "Client", client_for_fixture)
    return connection, opener, agent, browser, original


@pytest.mark.parametrize("handoff", [BASE, "http://127.0.0.1:8002"], indirect=True)
def test_panel_link_cli_issues_once_and_preserves_ticket_for_browser(handoff, capsys):
    connection, opener, agent, browser, original = handoff
    base = str(browser.base_url)
    home = base + "/#/me"
    assert helper.main(["--connection", str(connection), "panel-link"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert original not in output.out
    payload = json.loads(output.out)
    result = payload["result"]
    assert set(result) == {"home", "login_url"}
    assert result["home"] == home and payload["update_required"] is False
    assert opener.requests == [
        (CAPABILITIES, "GET", None, None),
        ("/api/agent-skill", "GET", None, None),
        ("/api/agent/context", "GET", "Bearer " + original, None),
        (ISSUE, "POST", "Bearer " + original, None),
    ]
    assert not agent.cookies
    assert not browser.get("/api/personal-panel/session").json()["authenticated"]
    ticket = result["login_url"].removeprefix(home + "?ticket=")
    login = browser.post(
        "/api/personal-panel/login", headers={"Origin": base}, json={"mode": "replace", "ticket": ticket}
    )
    assert login.status_code == 200
    assert browser.get("/api/personal-panel/session").json()["authenticated"]
    assert (connection.parent / "resource.key").read_text() == original
    assert ticket not in connection.read_text()


@pytest.mark.parametrize(
    "change",
    [
        {"protocol": "future"},
        {"issue_path": "/api/personal-panel/credential-view-links"},
        {"home": "https://other.example.test/#/me"},
        {"transport": "private-http"},
        {"connection_management": {"version": 2}},
        {"connection_management": {"version": True, "list_path": "/api/personal-panel/connections"}},
    ],
)
def test_unsupported_capability_never_reads_key_or_issues_link(handoff, change):
    connection, opener, *_ = handoff
    opener.capability_change = change
    (connection.parent / "resource.key").unlink()
    with pytest.raises(helper.ClientError, match="native_personal_panel_unavailable"):
        helper.Client(connection).panel_link()
    assert opener.requests == [(CAPABILITIES, "GET", None, None)]


@pytest.mark.parametrize("field", ["instance_id", "bot_uuid", "actor"])
def test_panel_link_checks_pinned_connection(handoff, field):
    connection, opener, *_ = handoff
    data = json.loads(connection.read_text())
    data[field] = {"platform": "infoflow", "external_user_id": "someone-else"} if field == "actor" else "changed"
    write_private(connection, data)
    with pytest.raises(helper.ClientError, match="mismatch"):
        helper.Client(connection).panel_link()
    assert all(method == "GET" for _, method, _, _ in opener.requests)


@pytest.mark.parametrize(
    "change",
    [
        {"home": HOME + "/another"},
        {"expires_in": True},
        {"expires_in": 0},
        {"expires_in": 121},
        {"login_url": f"https://other.example.test/#/me?ticket={'x' * 43}"},
        {"login_url": f"{BASE}/?ticket={'x' * 43}#/me"},
        {"login_url": f"{HOME}?ticket={'x' * 43}&ticket=two"},
        {"login_url": f"{HOME}?ticket={'x' * 43}&mode=append"},
        {"login_url": f"{HOME}?ticket=lbr_{'x' * 43}"},
    ],
)
def test_malformed_link_never_leaves_cli_or_triggers_reissue(handoff, change, capsys):
    connection, opener, *_ = handoff
    opener.issue_change = change
    assert helper.main(["--connection", str(connection), "panel-link"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "invalid_personal_panel_link", "retry_mutation": False}
    assert sum(method == "POST" for _, method, _, _ in opener.requests) == 1


@pytest.mark.parametrize("status", [429, 503])
def test_issue_error_is_not_retried(handoff, status, capsys):
    connection, opener, *_ = handoff
    opener.issue_status = status
    assert helper.main(["--connection", str(connection), "panel-link"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["retry_mutation"] is False
    assert sum(method == "POST" for _, method, _, _ in opener.requests) == 1


@pytest.mark.parametrize("origin", ["http://panel.example.test", "http://198.51.100.10"])
def test_panel_can_never_send_key_over_public_http(handoff, origin):
    connection, opener, *_ = handoff
    data = json.loads(connection.read_text())
    write_private(connection, {**data, "approved_origin": origin})
    with pytest.raises(helper.ClientError, match="personal_panel_transport_refused"):
        helper.Client(connection).panel_link()
    assert not opener.requests
