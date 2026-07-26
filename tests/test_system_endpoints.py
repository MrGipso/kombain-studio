from fastapi.testclient import TestClient

from backend.main import app


def test_health_and_build_info_are_typed_and_available():
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        info = client.get("/api/build-info")
        assert info.status_code == 200
        payload = info.json()
        assert payload["app"] == "Комбайн Studio"
        assert payload["project_schema"] >= 1
        assert payload["manifest_schema"] >= 1


def test_readiness_reports_named_checks():
    with TestClient(app) as client:
        response = client.get("/api/readiness")
        assert response.status_code in {200, 503}
        assert set(response.json()["checks"]) == {"project_storage", "frontend"}
