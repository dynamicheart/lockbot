from fastapi.testclient import TestClient
from lockbot.backend.app.database import get_db
from lockbot.backend.app.main import create_app


def test_retired_panel_returns_gone_even_with_an_old_configuration(monkeypatch, tmp_path, session_factory):
    monkeypatch.setenv("LOCKBOT_PERSONAL_PANEL_CONFIG", str(tmp_path / "old-config.json"))
    monkeypatch.setenv("FRONTEND_DIST", str(tmp_path))
    (tmp_path / "index.html").write_text("<html>LockBot</html>")

    app = create_app()

    def test_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = test_db
    with TestClient(app) as client:
        for method, suffix in (("GET", ""), ("GET", "/capabilities"), ("GET", "/session"), ("POST", "/links")):
            response = client.request(method, "/api/personal-panel" + suffix)
            assert response.status_code == 410
            assert response.headers["cache-control"] == "no-store"
        assert client.get("/").status_code == 200
        assert client.get("/api/agent/v2/manifest").json()["api_version"] == "2"
