"""Run the distributed helper against real resource API/core fixtures, not invented bot IDs."""

# ruff: noqa: F811
import io
import json
import urllib.error
import zipfile

import pytest
from lockbot.backend.app.resource_api.skill import skill_artifacts

from backend.test_resource_agent_helper import helper, write_private
from backend.test_resource_api_resources import api  # noqa: F401


@pytest.mark.parametrize("api", ["NODE", "DEVICE", "QUEUE", "QUEUE_DEVICE"], indirect=True)
def test_helper_real_api_init_acquire_renew_release(client, api, tmp_path):
    class Bridge:
        def open(self, request, timeout):
            result = client.request(
                request.method, request.full_url, headers=dict(request.header_items()), content=request.data
            )
            if result.status_code >= 400:
                raise urllib.error.HTTPError(
                    request.full_url, result.status_code, "refused", result.headers, io.BytesIO(result.content)
                )
            payload = io.BytesIO(result.content)
            payload.headers = result.headers
            return payload

    _, bundle, meta = skill_artifacts()
    with zipfile.ZipFile(io.BytesIO(bundle)) as zipped:
        zipped.extractall(tmp_path)
    root = tmp_path / meta["name"]
    key = write_private(tmp_path / "existing.key", api["headers"]["Authorization"].split(" ", 1)[1])
    original = key.read_bytes()
    connection = tmp_path / "connection.json"
    init = helper.Client(approved_origin="http://testserver", root=root, opener=Bridge())
    scope = init.initialize(key, connection)
    assert scope["bot"]["id"] == api["bot_id"]
    assert key.read_bytes() == original
    cards = ["0"] if scope["bot"]["type"] in {"DEVICE", "QUEUE_DEVICE"} else []
    for operation, duration in [("lock", "2m"), ("renew", "3m"), ("release", None)]:
        session = helper.Client(connection, root=root, opener=Bridge())
        result = session.command(operation, ["n1"], cards, duration, {"task_category": "unit_testing"})
        assert result["verified"] and not result["job_runner"]
    if scope["bot"]["type"] in {"QUEUE", "QUEUE_DEVICE"}:
        for operation, duration in [("book", "2m"), ("release", None)]:
            session = helper.Client(connection, root=root, opener=Bridge())
            assert session.command(operation, ["n1"], cards, duration)["verified"]
    assert json.loads(connection.read_text())["bot_uuid"] == scope["bot"]["uuid"]
