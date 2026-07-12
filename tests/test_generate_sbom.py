from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from tools import generate_sbom


def _write_fixture(root: Path) -> None:
    (root / "frontend").mkdir()
    (root / "pyproject.toml").write_text(
        """\
[project]
name = "Example_App"
version = "2.0.0"
description = "Example release"
dependencies = ["demo-dep>=1"]
""",
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        """\
version = 1
revision = 3
requires-python = ">=3.12"

[[package]]
name = "demo_dep"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.example/demo_dep-1.2.3.tar.gz", hash = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", size = 10 }

[[package]]
name = "example-app"
version = "2.0.0"
source = { virtual = "." }
dependencies = [{ name = "demo-dep" }]
""",
        encoding="utf-8",
    )
    npm_digest = hashlib.sha512(b"locked npm artifact").digest()
    integrity = "sha512-" + base64.b64encode(npm_digest).decode("ascii")
    package_lock = {
        "name": "web-client",
        "version": "3.0.0",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "": {"name": "web-client", "version": "3.0.0"},
            "node_modules/@scope/widget": {
                "version": "4.5.6",
                "resolved": "https://registry.example/widget.tgz",
                "integrity": integrity,
                "dev": True,
                "license": "MIT",
            },
        },
    }
    (root / "frontend" / "package-lock.json").write_text(
        json.dumps(package_lock), encoding="utf-8"
    )


def test_generate_sbom_is_deterministic_and_contains_both_ecosystems(tmp_path: Path) -> None:
    _write_fixture(tmp_path)

    first = generate_sbom.generate_sbom(tmp_path)
    second = generate_sbom.generate_sbom(tmp_path)

    assert generate_sbom.serialize_sbom(first) == generate_sbom.serialize_sbom(second)
    assert first["bomFormat"] == "CycloneDX"
    assert first["specVersion"] == "1.5"
    assert first["serialNumber"].startswith("urn:uuid:")
    assert "timestamp" not in first["metadata"]
    assert first["metadata"]["component"]["purl"] == "pkg:pypi/example-app@2.0.0"

    components = {component["purl"]: component for component in first["components"]}
    assert list(components) == sorted(components)
    assert "pkg:pypi/example-app@2.0.0" not in components

    python_component = components["pkg:pypi/demo-dep@1.2.3"]
    assert python_component["hashes"] == [{"alg": "SHA-256", "content": "a" * 64}]
    assert python_component["externalReferences"][0]["type"] == "distribution"

    npm_component = components["pkg:npm/%40scope/widget@4.5.6"]
    assert npm_component["hashes"] == [
        {
            "alg": "SHA-512",
            "content": hashlib.sha512(b"locked npm artifact").hexdigest(),
        }
    ]
    assert npm_component["licenses"] == [{"license": {"name": "MIT"}}]
    assert components["pkg:npm/web-client@3.0.0"]["type"] == "application"

    properties = first["metadata"]["properties"]
    assert len(properties) == 3
    assert all(prop["name"].endswith(":sha256") for prop in properties)
    assert all(len(prop["value"]) == 64 for prop in properties)


def test_input_change_changes_reproducible_serial_number(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    before = generate_sbom.generate_sbom(tmp_path)["serialNumber"]

    npm_lock = tmp_path / "frontend" / "package-lock.json"
    npm_lock.write_text(npm_lock.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    after = generate_sbom.generate_sbom(tmp_path)["serialNumber"]
    assert after != before


def test_write_atomic_preserves_old_output_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "sbom.json"
    output.write_text("old", encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(generate_sbom.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        generate_sbom.write_atomic(output, "new")

    assert output.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".sbom.json.*.tmp")) == []


def test_cli_writes_relative_to_repository_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_fixture(tmp_path)

    result = generate_sbom.main(
        ["--root", str(tmp_path), "--output", "release/example.cdx.json"]
    )

    output = tmp_path / "release" / "example.cdx.json"
    assert result == 0
    assert json.loads(output.read_text(encoding="utf-8"))["bomFormat"] == "CycloneDX"
    assert "Wrote 3 components" in capsys.readouterr().out


def test_cli_refuses_to_overwrite_an_input(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    original = (tmp_path / "uv.lock").read_bytes()

    with pytest.raises(SystemExit) as error:
        generate_sbom.main(
            ["--root", str(tmp_path), "--output", str(tmp_path / "uv.lock")]
        )

    assert error.value.code == 2
    assert (tmp_path / "uv.lock").read_bytes() == original


def test_repository_lockfiles_generate_without_environment_lookup() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    document = generate_sbom.generate_sbom(repository_root)

    purls = [component["purl"] for component in document["components"]]
    assert len(purls) > 100
    assert any(purl.startswith("pkg:pypi/") for purl in purls)
    assert any(purl.startswith("pkg:npm/") for purl in purls)
    assert len(purls) == len(set(purls))
    assert hashlib.sha256(generate_sbom.serialize_sbom(document).encode()).hexdigest()
