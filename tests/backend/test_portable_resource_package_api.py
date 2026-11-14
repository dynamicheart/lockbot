"""Exercise two deployment renderings against real API headers and core mutation."""

# ruff: noqa: F811
import io
import urllib.error

from backend.test_portable_resource_package import extract
from backend.test_resource_agent_helper import helper, write_private
from backend.test_resource_api_resources import api  # noqa: F401


def test_two_origin_rendered_bundles_use_package_compatibility_not_shared_identity(client, api, tmp_path, monkeypatch):
    installed = extract(tmp_path / "installed", "https://first.example.test")
    monkeypatch.setenv("PLATFORM_URL", "http://testserver")

    class Bridge:
        def open(self, request, timeout):
            response = client.request(
                request.method, request.full_url, headers=dict(request.header_items()), content=request.data
            )
            if response.status_code >= 400:
                raise urllib.error.HTTPError(
                    request.full_url, response.status_code, "refused", response.headers, io.BytesIO(response.content)
                )
            result = io.BytesIO(response.content)
            result.headers = response.headers
            return result

    key = write_private(tmp_path / "resource.key", api["headers"]["Authorization"].split(" ", 1)[1])
    saved = tmp_path / "connection.json"
    init = helper.Client(approved_origin="http://testserver", root=tmp_path / "installed", opener=Bridge())
    context = init.initialize(key, saved)
    assert not init.needs_update
    assert context["bot"]["id"] == api["bot_id"]
    before = key.read_bytes(), saved.read_bytes()
    session = helper.Client(saved, root=tmp_path / "installed", opener=Bridge())
    assert session.command("lock", ["n1"], [], "2m")["verified"]
    assert before == (key.read_bytes(), saved.read_bytes())
    response = client.get(
        "/api/agent/context",
        headers={
            **api["headers"],
            "X-Lockbot-Client-Skill-Version": installed["version"],
            "X-Lockbot-Client-Package-Version": installed["package_version"],
        },
    )
    assert response.headers["x-lockbot-skill-update"] == "update_available"  # existing rendered-byte semantics
    assert response.headers["x-lockbot-package-update"] == "current"
    assert response.headers["x-lockbot-package-version"] == installed["package_version"]
    assert response.json()["skill_update"]["package_status"] == "current"
    assert response.json()["skill_update"]["status"] == "update_available"
    assert response.json()["skill_update"]["instructions"] is None
