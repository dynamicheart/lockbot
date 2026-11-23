import json

from lockbot.backend.app.auth.dependencies import create_access_token
from lockbot.backend.app.auth.models import User
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
    assert response.json() == snapshot
    assert response.headers["Cache-Control"] == "no-store"
    assert client.post(path, headers=headers, json=snapshot).status_code == 405

    raw = {"schema": 1, "visibility": "raw", "cases": {}, "evidence": "x" * (1024 * 1024)}
    (tmp_path / "raw.json").write_text(json.dumps(raw))
    response = client.get(raw_path, headers=headers)
    assert response.json() == raw
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get(path, headers=headers).json() == snapshot

    user.role = "admin"
    db_session.commit()
    assert client.get(path, headers=headers).status_code == 403
    assert client.get(raw_path, headers=headers).status_code == 403
