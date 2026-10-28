"""Minimal service health endpoint; permissions are enforced by individual APIs."""


def install_health_endpoint(app):
    @app.get("/api/health", include_in_schema=False)
    def health():
        return {"status": "ok"}
