import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lockbot.backend.app.auth.dependencies import create_access_token
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.trace_review import trace_review_router
from sqlalchemy import select


def test_trace_review_routes_are_opt_in(request, monkeypatch):
    monkeypatch.delenv("LOCKBOT_TRACE_REVIEW_DIR", raising=False)
    client = request.getfixturevalue("client")
    assert client.get("/api/dev/trace-review/snapshot").status_code == 404


def test_trace_snapshot_requires_current_super_admin(request, tmp_path, monkeypatch, db_session):
    monkeypatch.setenv("LOCKBOT_TRACE_REVIEW_DIR", str(tmp_path))
    (tmp_path / "viewer.html").write_text("<title>Trace review</title>")
    client = request.getfixturevalue("client")
    user_headers = request.getfixturevalue("auth_header")
    path = "/api/dev/trace-review/snapshot"
    assert client.get("/dev/trace-review").status_code == 200
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
    raw_path = path + "?raw=true"
    assert client.get(raw_path).status_code == 401
    assert client.get(raw_path, headers=user_headers).status_code == 403
    assert client.get(path, headers=user_headers).status_code == 403

    user = db_session.execute(select(User).where(User.username == "testuser")).scalar_one()
    user.role = "super_admin"
    db_session.commit()
    headers = {"Authorization": "Bearer " + create_access_token(user.id, user.effective_token_version)}
    assert client.get(path, headers=headers).status_code == 404
    assert client.get(raw_path, headers=headers).status_code == 404
    snapshot = {"schema": 1, "title": "synthetic summary", "cases": {}}
    (tmp_path / "latest.json").write_text(json.dumps(snapshot))
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.json() == {**snapshot, "review_runs": {}}
    assert response.headers["Cache-Control"] == "no-store"
    assert client.post(path, headers=headers, json=snapshot).status_code == 405

    raw = {"schema": 1, "visibility": "raw", "cases": {}, "evidence": "x" * (1024 * 1024)}
    (tmp_path / "raw.json").write_text(json.dumps(raw))
    response = client.get(raw_path, headers=headers)
    assert response.json() == {**raw, "review_runs": {}}
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get(path, headers=headers).json() == {**snapshot, "review_runs": {}}

    user.role = "admin"
    db_session.commit()
    assert client.get(path, headers=headers).status_code == 403
    assert client.get(raw_path, headers=headers).status_code == 403


def test_public_settings_expose_trace_entry_only_when_the_viewer_exists(client, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKBOT_TRACE_REVIEW_DIR", str(tmp_path))
    response = client.get("/api/settings")
    assert response.status_code == 200
    assert not any(setting["key"] == "agent_trace_review_url" for setting in response.json())
    (tmp_path / "viewer.html").write_text("<html>test viewer</html>")
    settings = {setting["key"]: setting["value"] for setting in client.get("/api/settings").json()}
    assert settings["agent_trace_review_url"] == "/#/agent-traces"
    assert str(tmp_path) not in str(settings)
    monkeypatch.delenv("LOCKBOT_TRACE_REVIEW_DIR")
    assert not any(setting["key"] == "agent_trace_review_url" for setting in client.get("/api/settings").json())


@pytest.fixture
def review_api(request, tmp_path, monkeypatch, db_session):
    monkeypatch.setenv("LOCKBOT_TRACE_REVIEW_DIR", str(tmp_path))
    (tmp_path / "viewer.html").write_text("<title>Review</title>")
    trial = {
        "model": "same-model",
        "sessions": [
            [
                {"role": "user", "text": "lock 19"},
                {
                    "role": "tools",
                    "steps": [["run_lockbot", "resources", "", "已返回"]],
                    "evidence": [{"input": {"action": "resources"}, "output": {"resources": []}}],
                },
                {"role": "assistant", "text": "请先确认目标机器"},
            ]
        ],
    }
    snapshot = {"schema": 1, "visibility": "raw", "version": "test", "cases": {"lock-tail": [trial, trial]}}
    (tmp_path / "raw.json").write_text(json.dumps(snapshot))
    client = request.getfixturevalue("client")
    request.getfixturevalue("auth_header")
    user = db_session.execute(select(User).where(User.username == "testuser")).scalar_one()
    user.role = "super_admin"
    db_session.commit()
    headers = {"Authorization": "Bearer " + create_access_token(user.id, user.effective_token_version)}
    run_id = client.get("/api/dev/trace-review/snapshot?raw=true", headers=headers).json()["review_runs"]["lock-tail"][
        0
    ]
    identities = client.get("/api/dev/trace-review/snapshot?raw=true", headers=headers).json()["review_runs"][
        "lock-tail"
    ]
    assert identities[0] != identities[1]
    payload = {
        "case_id": "lock-tail",
        "run_index": 0,
        "run_id": run_id,
        "anchor": "message:0:2:text",
        "quote": "确认目标机器",
        "body": "应该先按尾号查机器。",
    }
    return client, headers, payload, snapshot, tmp_path, user


def test_comments_require_current_super_admin_and_server_authorship(review_api, db_session):
    client, headers, payload, _, _, user = review_api
    path = "/api/dev/trace-review/comments"
    assert client.get(path).status_code == 401
    assert client.post(path, json=payload).status_code == 401
    assert client.post(path, headers=headers, json={**payload, "author": "someone-else"}).status_code == 422
    response = client.post(path, headers=headers, json=payload)
    assert response.status_code == 201
    record = response.json()
    assert record["author"] == "testuser" and record["body"] == payload["body"]
    user.role = "admin"
    db_session.commit()
    assert client.get(path, headers=headers).status_code == 403
    assert client.post(path, headers=headers, json=payload).status_code == 403
    assert client.patch(path + "/" + record["id"], headers=headers, json={"status": "resolved"}).status_code == 403


def test_case_feedback_is_independent_of_models_and_reruns(review_api):
    client, headers, payload, snapshot, directory, _ = review_api
    path = "/api/dev/trace-review/comments"
    case = {key: value for key, value in payload.items() if key not in {"run_index", "run_id"}}
    case.update(scope="case", anchor="case", subject="prompt")
    created = client.post(path, headers=headers, json=case)
    assert created.status_code == 201
    record = created.json()
    assert record["model"] is None and record["run_id"] is None and record["run_index"] is None
    # Keep the case, replace every model and its evidence, then rebuild the router.
    snapshot["cases"]["lock-tail"] = [{"model": "new-model", "sessions": []}]
    (directory / "raw.json").write_text(json.dumps(snapshot))
    fresh = FastAPI()
    fresh.dependency_overrides.update(client.app.dependency_overrides)
    fresh.include_router(trace_review_router(directory))
    with TestClient(fresh) as reopened:
        result = reopened.get(path, headers=headers, params={"case_id": "lock-tail"}).json()
        assert result["comments"] == [record]
        updated = reopened.patch(path + "/" + record["id"], headers=headers, json={"status": "resolved"})
        assert updated.status_code == 200 and updated.json()["status"] == "resolved"
    assert (directory / "review-comments.sqlite3").stat().st_mode & 0o777 == 0o600


def test_changed_evidence_rejects_stale_comment_but_retains_existing_feedback(review_api):
    client, headers, payload, snapshot, directory, _ = review_api
    path = "/api/dev/trace-review/comments"
    saved = client.post(path, headers=headers, json=payload).json()
    snapshot["cases"]["lock-tail"][0]["sessions"][0][-1]["text"] = "现在查到了另一台机器"
    (directory / "raw.json").write_text(json.dumps(snapshot))
    assert client.post(path, headers=headers, json=payload).status_code == 409
    result = client.get(path, headers=headers, params={"case_id": "lock-tail", "run_index": 0}).json()
    assert result["run_id"] != saved["run_id"]
    assert result["comments"] == [saved]


@pytest.mark.parametrize(
    "anchor", ["message:0:99:text", "tool:0:0:0:input", "tool:0:1:8:output", "ui:not-a-field", "../../file"]
)
def test_unknown_or_unrelated_anchor_is_rejected(review_api, anchor):
    client, headers, payload, _, _, _ = review_api
    assert (
        client.post("/api/dev/trace-review/comments", headers=headers, json={**payload, "anchor": anchor}).status_code
        == 422
    )


def test_case_feedback_cannot_silently_attach_to_a_model_run(review_api):
    client, headers, payload, _, _, _ = review_api
    assert (
        client.post(
            "/api/dev/trace-review/comments", headers=headers, json={**payload, "scope": "case", "anchor": "case"}
        ).status_code
        == 422
    )
