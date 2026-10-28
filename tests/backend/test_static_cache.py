import os

from fastapi import FastAPI
from fastapi.testclient import TestClient
from lockbot.backend.app.static import SPAStaticFiles


def test_html_does_not_reuse_equal_size_mtime_validators(tmp_path):
    p = tmp_path / "index.html"
    p.write_text("old")
    os.utime(p, (0, 0))
    app = FastAPI()
    app.mount("/", SPAStaticFiles(directory=tmp_path, html=True))
    client = TestClient(app)
    first = client.get("/")
    assert first.headers["cache-control"] == "no-store"
    p.write_text("new")
    os.utime(p, (0, 0))
    response = client.get("/", headers={"If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT", "If-None-Match": "*"})
    assert response.status_code == 200 and response.text == "new"
    assert "etag" not in response.headers
    assert client.get("/bots/4", headers={"Accept": "text/html"}).text == "new"
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/api/missing", headers={"Accept": "text/html"}).status_code == 404
