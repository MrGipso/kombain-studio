"""Generate a deterministic, offline CycloneDX dependency inventory.

The generator deliberately reads only source-controlled package metadata.  It
does not inspect the active virtual environment, invoke package managers, or
contact registries, so the same lockfiles always produce the same document.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote
import uuid


CYCLONEDX_SPEC_VERSION = "1.5"
DEFAULT_OUTPUT = Path("artifacts/pipeline-studio.cdx.json")
INPUT_PATHS = (
    Path("pyproject.toml"),
    Path("uv.lock"),
    Path("frontend/package-lock.json"),
)
_PYPI_NORMALIZE_RE = re.compile(r"[-_.]+")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_HASH_ALGORITHMS = {
    "md5": "MD5",
    "sha1": "SHA-1",
    "sha256": "SHA-256",
    "sha384": "SHA-384",
    "sha512": "SHA-512",
}
_HASH_STRENGTH = {"sha512": 5, "sha384": 4, "sha256": 3, "sha1": 2, "md5": 1}
_HASH_HEX_LENGTHS = {"md5": 32, "sha1": 40, "sha256": 64, "sha384": 96, "sha512": 128}


def _pypi_name(name: str) -> str:
    """Return the normalized distribution name used by the PyPI purl type."""

    return _PYPI_NORMALIZE_RE.sub("-", name).lower()


def _quoted(value: str) -> str:
    return quote(value, safe=".-_~")


def _pypi_purl(name: str, version: str) -> str:
    return f"pkg:pypi/{_quoted(_pypi_name(name))}@{_quoted(version)}"


def _npm_purl(name: str, version: str) -> str:
    if name.startswith("@") and "/" in name:
        namespace, package_name = name[1:].split("/", 1)
        package_path = f"%40{_quoted(namespace)}/{_quoted(package_name)}"
    else:
        package_path = _quoted(name)
    return f"pkg:npm/{package_path}@{_quoted(version)}"


def _hash_record(value: object) -> dict[str, str] | None:
    """Convert uv's ``algorithm:hex`` value to a CycloneDX hash record."""

    if not isinstance(value, str) or ":" not in value:
        return None
    algorithm, content = value.split(":", 1)
    normalized_algorithm = _HASH_ALGORITHMS.get(algorithm.lower())
    if (
        normalized_algorithm is None
        or not content
        or len(content) != _HASH_HEX_LENGTHS[algorithm.lower()]
        or _HEX_RE.fullmatch(content) is None
    ):
        return None
    return {"alg": normalized_algorithm, "content": content.lower()}


def _sri_hash(integrity: object) -> dict[str, str] | None:
    """Convert the strongest valid npm SRI token to CycloneDX hexadecimal form."""

    if not isinstance(integrity, str):
        return None

    candidates: list[tuple[int, str, bytes]] = []
    for token in integrity.split():
        if "-" not in token:
            continue
        algorithm, encoded = token.split("-", 1)
        algorithm = algorithm.lower()
        if algorithm not in _HASH_ALGORITHMS:
            continue
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(raw.hex()) == _HASH_HEX_LENGTHS[algorithm]:
            candidates.append((_HASH_STRENGTH[algorithm], algorithm, raw))

    if not candidates:
        return None
    _, algorithm, raw = max(candidates, key=lambda item: (item[0], item[1], item[2]))
    return {"alg": _HASH_ALGORITHMS[algorithm], "content": raw.hex()}


def _distribution_fields(
    url: object, hash_record: dict[str, str] | None
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if hash_record is not None:
        result["hashes"] = [hash_record]
    if isinstance(url, str) and url:
        reference: dict[str, Any] = {"type": "distribution", "url": url}
        if hash_record is not None:
            reference["hashes"] = [hash_record]
        result["externalReferences"] = [reference]
    return result


def _uv_artifact(package: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Choose one stable distribution artifact, preferring the source archive."""

    sdist = package.get("sdist")
    if isinstance(sdist, Mapping) and _hash_record(sdist.get("hash")) is not None:
        return sdist

    wheels = package.get("wheels")
    if isinstance(wheels, list):
        valid = [
            item
            for item in wheels
            if isinstance(item, Mapping) and _hash_record(item.get("hash")) is not None
        ]
        wheel = min(valid, key=lambda item: str(item.get("url", "")), default=None)
        if wheel is not None:
            return wheel
    return sdist if isinstance(sdist, Mapping) else None


def _python_components(
    uv_data: Mapping[str, Any], root_name: str
) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    packages = uv_data.get("package", [])
    if not isinstance(packages, list):
        raise ValueError("uv.lock must contain [[package]] records")

    normalized_root = _pypi_name(root_name)
    for package in packages:
        if not isinstance(package, Mapping):
            continue
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        if _pypi_name(name) == normalized_root:
            continue

        purl = _pypi_purl(name, version)
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": purl,
            "name": _pypi_name(name),
            "version": version,
            "purl": purl,
            "properties": [
                {"name": "pipeline-studio:ecosystem", "value": "python"},
                {"name": "pipeline-studio:source-lockfile", "value": "uv.lock"},
            ],
        }
        artifact = _uv_artifact(package)
        if artifact is not None:
            component.update(
                _distribution_fields(
                    artifact.get("url"), _hash_record(artifact.get("hash"))
                )
            )
        components.append(component)
    return components


def _npm_name(package_path: str, package: Mapping[str, Any]) -> str | None:
    explicit_name = package.get("name")
    if isinstance(explicit_name, str) and explicit_name:
        return explicit_name
    marker = "node_modules/"
    if marker not in package_path:
        return None
    return package_path.rsplit(marker, 1)[1] or None


def _npm_component(
    name: str, package: Mapping[str, Any], *, application: bool = False
) -> dict[str, Any] | None:
    version = package.get("version")
    if not isinstance(version, str) or not version:
        return None
    purl = _npm_purl(name, version)
    properties = [
        {"name": "pipeline-studio:ecosystem", "value": "npm"},
        {
            "name": "pipeline-studio:source-lockfile",
            "value": "frontend/package-lock.json",
        },
    ]
    component: dict[str, Any] = {
        "type": "application" if application else "library",
        "bom-ref": purl,
        "name": name,
        "version": version,
        "purl": purl,
        "properties": properties,
    }
    license_name = package.get("license")
    if isinstance(license_name, str) and license_name:
        component["licenses"] = [{"license": {"name": license_name}}]
    component.update(
        _distribution_fields(package.get("resolved"), _sri_hash(package.get("integrity")))
    )
    return component


def _merge_component(existing: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    """Merge duplicate package-lock locations without creating duplicate bom-refs."""

    for field in ("hashes", "externalReferences", "licenses"):
        if field not in existing and field in incoming:
            existing[field] = incoming[field]

    existing_properties = {
        (item.get("name"), item.get("value"))
        for item in existing.get("properties", [])
        if isinstance(item, Mapping)
    }
    for item in incoming.get("properties", []):
        identity = (item.get("name"), item.get("value")) if isinstance(item, Mapping) else None
        if isinstance(item, Mapping) and identity not in existing_properties:
            existing.setdefault("properties", []).append(dict(item))
    existing["properties"].sort(key=lambda item: (item["name"], item["value"]))


def _npm_components(npm_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    packages = npm_data.get("packages")
    if not isinstance(packages, Mapping):
        raise ValueError("frontend/package-lock.json must contain a packages object")

    components: dict[str, dict[str, Any]] = {}
    for package_path, package in packages.items():
        if not isinstance(package_path, str) or not isinstance(package, Mapping):
            continue
        if package_path == "":
            name = _npm_name(package_path, package)
            if name is None:
                name_value = npm_data.get("name")
                name = name_value if isinstance(name_value, str) else None
            component = _npm_component(name, package, application=True) if name else None
        else:
            name = _npm_name(package_path, package)
            component = _npm_component(name, package) if name else None
        if component is None:
            continue
        existing = components.get(component["bom-ref"])
        if existing is None:
            components[component["bom-ref"]] = component
        else:
            _merge_component(existing, component)
    return list(components.values())


def _input_fingerprint(inputs: Iterable[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(inputs):
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def generate_sbom(root: Path) -> dict[str, Any]:
    """Build a CycloneDX 1.5 document from lockfiles below *root*."""

    root = root.resolve()
    raw_inputs = {path.as_posix(): (root / path).read_bytes() for path in INPUT_PATHS}
    pyproject = tomllib.loads(raw_inputs["pyproject.toml"].decode("utf-8"))
    uv_data = tomllib.loads(raw_inputs["uv.lock"].decode("utf-8"))
    npm_data = json.loads(raw_inputs["frontend/package-lock.json"].decode("utf-8"))

    project = pyproject.get("project")
    if not isinstance(project, Mapping):
        raise ValueError("pyproject.toml must contain a [project] table")
    project_name = project.get("name")
    project_version = project.get("version")
    if not isinstance(project_name, str) or not isinstance(project_version, str):
        raise ValueError("[project] must define string name and version fields")

    fingerprint = _input_fingerprint(raw_inputs.items())
    root_purl = _pypi_purl(project_name, project_version)
    root_component: dict[str, Any] = {
        "type": "application",
        "bom-ref": root_purl,
        "name": project_name,
        "version": project_version,
        "purl": root_purl,
    }
    description = project.get("description")
    if isinstance(description, str) and description:
        root_component["description"] = description

    components = _python_components(uv_data, project_name) + _npm_components(npm_data)
    components.sort(key=lambda component: component["bom-ref"])
    if len({component["bom-ref"] for component in components}) != len(components):
        raise ValueError("lockfiles produced duplicate CycloneDX bom-ref values")

    input_properties = [
        {
            "name": f"pipeline-studio:input:{name}:sha256",
            "value": hashlib.sha256(content).hexdigest(),
        }
        for name, content in sorted(raw_inputs.items())
    ]
    return {
        "$schema": "http://cyclonedx.org/schema/bom-1.5.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": CYCLONEDX_SPEC_VERSION,
        "serialNumber": "urn:uuid:"
        + str(uuid.uuid5(uuid.NAMESPACE_URL, f"pipeline-studio:sbom:{fingerprint}")),
        "version": 1,
        "metadata": {
            "tools": [
                {
                    "vendor": "Pipeline Studio",
                    "name": "tools/generate_sbom.py",
                }
            ],
            "component": root_component,
            "properties": input_properties,
        },
        "components": components,
    }


def serialize_sbom(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_atomic(output: Path, content: str) -> None:
    """Atomically replace *output*, preserving any old file on failure."""

    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=output.parent, prefix=f".{output.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, output)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an offline deterministic CycloneDX JSON SBOM."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root containing pyproject.toml, uv.lock and frontend/package-lock.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"output JSON path (default: {DEFAULT_OUTPUT.as_posix()})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = args.root.resolve()
    output = args.output
    if not output.is_absolute():
        output = root / output
    resolved_output = output.resolve()
    input_files = {(root / relative_path).resolve() for relative_path in INPUT_PATHS}
    if resolved_output in input_files:
        parser.error("--output must not overwrite an SBOM input file")
    try:
        document = generate_sbom(root)
        write_atomic(output, serialize_sbom(document))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Wrote {len(document['components'])} components to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
