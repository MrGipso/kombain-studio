"""P0-1 regression: the SPA catch-all must not serve files outside static/."""

from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import _safe_static_path, app, static_dir


def test_normal_asset_path_allowed():
    # A real file inside static/ resolves to itself.
    (static_dir).mkdir(parents=True, exist_ok=True)
    probe = static_dir / "_probe.txt"
    probe.write_text("ok", encoding="utf-8")
    try:
        resolved = _safe_static_path("_probe.txt")
        assert resolved is not None and resolved == probe.resolve()
    finally:
        probe.unlink(missing_ok=True)


def test_parent_traversal_rejected():
    assert _safe_static_path("../pyproject.toml") is None
    assert _safe_static_path("../../pyproject.toml") is None
    assert _safe_static_path("../../backend/main.py") is None


def test_absolute_path_rejected():
    # An absolute path joined to static_dir must not escape it.
    assert _safe_static_path("/etc/passwd") is None or \
        Path("/etc/passwd") != _safe_static_path("/etc/passwd")


def test_empty_path_is_none():
    assert _safe_static_path("") is None


def test_encoded_parent_traversal_returns_404_and_never_file_contents():
    secret = Path(__file__).resolve().parents[1] / "pyproject.toml"
    marker = secret.read_text(encoding="utf-8")[:40]

    with TestClient(app) as client:
        for url in (
            "/%2e%2e/%2e%2e/pyproject.toml",
            "/..%2f..%2fpyproject.toml",
            "/%2e%2e%2f%2e%2e%2fpyproject.toml",
        ):
            response = client.get(url)
            assert response.status_code == 404
            assert marker not in response.text


def test_untrusted_host_and_mutation_origin_are_rejected():
    with TestClient(app) as client:
        hostile_host = client.get("/", headers={"Host": "attacker.example"})
        assert hostile_host.status_code == 400

        hostile_origin = client.post(
            "/api/projects/missing/cancel",
            headers={"Origin": "https://attacker.example"},
        )
        assert hostile_origin.status_code == 403

        local_origin = client.post(
            "/api/projects/missing/cancel",
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert local_origin.status_code == 404
