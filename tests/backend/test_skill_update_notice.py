# ruff: noqa: F811
from lockbot.backend.app.resource_api.skill import skill_artifacts

from backend.test_resource_api_resources import api  # noqa: F401


def test_legacy_client_sees_update_in_json_without_losing_command_result(client, api):
    path = f"/api/bots/{api['bot_id']}"
    for method, endpoint, body in (
        ("get", "/api/agent/context", None),
        ("get", path + "/resources", None),
        ("post", path + "/commands", {"command": "lock n1 1h"}),
    ):
        kwargs = {"headers": api["headers"]}
        if body is not None:
            kwargs["json"] = body
        response = getattr(client, method)(endpoint, **kwargs)
        assert response.status_code == 200, response.text
        notice = response.json()["skill_update"]
        assert notice["status"] == "unknown" and notice["reported_version"] is None
        assert notice["skill_url"] == "/api/agent-skill/SKILL.md" and notice["instructions"]
    assert api["bot"].state.bot_state["n1"]["current_users"][0]["user_id"] == "alice"


def test_client_reported_revision_is_compared_not_trusted_as_installation_proof(client, api):
    revision = skill_artifacts()[2]["version"]
    for reported, expected in ((revision, "current"), ("0" * 16, "update_available"), ("untrusted-version", "unknown")):
        headers = {**api["headers"], "X-Lockbot-Client-Skill-Version": reported}
        response = client.get("/api/agent/context", headers=headers)
        assert response.status_code == 200
        notice = response.json()["skill_update"]
        assert notice["status"] == expected and notice["current_version"] == revision
        assert notice["reported_version"] == (None if expected == "unknown" else reported)
        assert (notice["instructions"] is None) == (expected == "current")
        assert "untrusted-version" not in response.text


def test_every_personal_api_response_signals_revision_even_on_errors(client, api):
    revision = skill_artifacts()[2]["version"]
    endpoints = [
        "/api/agent/context",
        f"/api/bots/{api['bot_id']}/resources",
        f"/api/bots/{api['bot_id']}/usage-declarations",
        "/api/resource-auth/bindings",
    ]
    for supplied, status in [(revision, "current"), ("0" * 16, "update_available"), ("bad", "unknown")]:
        for endpoint in endpoints:
            # Invalid credentials deliberately exercise the rejected-request path.
            response = client.get(
                endpoint,
                headers={
                    "Authorization": "Bearer lbr_invalid",
                    "X-Lockbot-Client-Skill-Version": supplied,
                    "X-Lockbot-Client-API-Version": "2",
                },
            )
            # Missing/method-mismatched routes can be 404 or 405 depending on SPA mounting.
            assert 400 <= response.status_code < 500
            assert response.headers["x-lockbot-skill-update"] == status
            assert response.headers["x-lockbot-api-compatibility"] == "incompatible"
            assert response.headers["x-lockbot-skill-url"] == "/api/agent-skill/SKILL.md"
            assert response.headers["x-lockbot-skill-version"] == revision
    response = client.get(
        "/api/agent/context",
        headers={
            **api["headers"],
            "X-Lockbot-Client-API-Version": "1",
            "X-Lockbot-Client-Skill-Version": revision,
        },
    )
    assert response.status_code == 200
    assert response.headers["x-lockbot-api-compatibility"] == "current"
    assert response.headers["x-lockbot-skill-update"] == "current"
