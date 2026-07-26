"""Run an isolated, inexpensive Комбайн Studio golden end-to-end check.

The harness deliberately does not use the configured projects directory, jobs
database, or server lock.  It starts a one-off backend with a minimal settings
file under a fresh run directory, synthesises a short spoken fixture through
Windows ``System.Speech``, and drives the public HTTP API through an immutable
Master export.  ComfyUI, stock providers, Woosh, and LM Studio are not required:
the production scene/shot heuristic fallbacks are exercised intentionally.

The run directory is retained as release evidence and printed on completion.
No existing project or settings file is modified.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from typing import Any, Iterable
import urllib.error
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_SOURCE_SETTINGS = ROOT / "content_pipeline_settings.json"
DEFAULT_SPEECH = (
    "Комбайн Studio turns a short narration into a verified professional "
    "video export."
)
REQUIRED_STAGES = (
    "transcript",
    "scenes",
    "shots",
    "assets",
    "timeline",
    "validation",
    "export",
)
EXPECTED_JOB_RESOURCES = Counter({"whisper": 1, "llm": 2, "ffmpeg": 1})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class GoldenError(RuntimeError):
    """A failed golden contract with a concise release-facing explanation."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_content_hash(value: Any) -> str:
    """Match ``core.project.content_hash`` for JSON-compatible values."""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def is_sha256(value: Any) -> bool:
    return bool(SHA256_RE.fullmatch(str(value or "").strip().lower()))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GoldenError(message)


def powershell_literal(value: str | Path) -> str:
    """Return a safe PowerShell single-quoted literal."""
    return "'" + str(value).replace("'", "''") + "'"


def encoded_powershell_command(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def parse_sse_text(text: str) -> list[dict[str, Any]]:
    """Parse the backend's one-JSON-object-per-``data:`` SSE representation."""
    events: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        raw_payload = line[5:].strip()
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise GoldenError(f"Malformed SSE JSON on line {line_number}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
            raise GoldenError(f"Malformed SSE event on line {line_number}")
        events.append(payload)
    if not events:
        raise GoldenError("SSE response contained no events")
    return events


def require_successful_sse(
    events: list[dict[str, Any]], operation: str
) -> dict[str, Any]:
    errors = [event for event in events if event.get("event") == "error"]
    if errors:
        detail = errors[-1].get("data")
        raise GoldenError(
            f"{operation} failed: {json.dumps(detail, ensure_ascii=False, sort_keys=True)}"
        )
    done = [event for event in events if event.get("event") == "done"]
    if not done:
        raise GoldenError(f"{operation} ended without a terminal done event")
    data = done[-1].get("data")
    if not isinstance(data, dict):
        raise GoldenError(f"{operation} returned malformed done data")
    return data


def multipart_body(
    *,
    field: str,
    filename: str,
    content_type: str,
    content: bytes,
    boundary: str,
) -> bytes:
    """Build the small, deterministic multipart body used for the speech WAV."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", boundary):
        raise ValueError("multipart boundary must be a safe token")
    safe_filename = Path(filename).name.replace('"', "") or "fixture.wav"
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{safe_filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    return header + content + f"\r\n--{boundary}--\r\n".encode("ascii")


def load_source_settings(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GoldenError(
            f"Source settings not found: {path}. Pass --source-settings."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldenError(f"Source settings are unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise GoldenError("Source settings must contain a JSON object")
    return payload


def build_isolated_settings(
    source: dict[str, Any], projects_dir: Path
) -> dict[str, Any]:
    """Copy only the external tool coordinates needed by this golden run."""
    whisper_cli = str(source.get("whisper_cli", "") or "").strip()
    whisper_model = str(source.get("whisper_model", "") or "").strip()
    if not whisper_cli or not whisper_model:
        raise GoldenError("Whisper CLI and model must be configured in source settings")
    return {
        "output_base": str(projects_dir.resolve()),
        "ffmpeg": str(source.get("ffmpeg", "") or "ffmpeg").strip() or "ffmpeg",
        "whisper_cli": whisper_cli,
        "whisper_model": whisper_model,
        "whisper_language": "en",
        # Port 9 is intentionally closed locally.  Connection refusal makes the
        # production scene/shot heuristic fallback deterministic and immediate.
        "lm_url": "http://127.0.0.1:9/v1",
        "lm_model": "offline-golden",
        "lm_embed_model": "offline-golden-embeddings",
        "lm_auto_unload": False,
        "reference_enable": False,
        "reference_density": "off",
        "sfx_enabled": False,
        "export_loudnorm": False,
        "music_normalize": False,
        "export_unify_grade": False,
        "export_grain": 0,
        "ai_frame_qa": False,
        "stock_free_sources": False,
        "ltx_draft_mode": "off",
    }


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def resolve_executable(value: str, fallback: str | None = None) -> str:
    configured = str(value or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        if not candidate.is_absolute():
            repository_candidate = ROOT / candidate
            if repository_candidate.is_file():
                return str(repository_candidate.resolve())
        discovered = shutil.which(configured)
        if discovered:
            return discovered
    if fallback:
        discovered = shutil.which(fallback)
        if discovered:
            return discovered
    raise GoldenError(f"Executable not found: {configured or fallback or '(empty)'}")


def synthesise_speech(path: Path, text: str = DEFAULT_SPEECH) -> None:
    if os.name != "nt":
        raise GoldenError("System.Speech golden fixture requires Windows")
    # System.Speech voice registration is exposed reliably through Windows
    # PowerShell/.NET Framework. PowerShell 7 can load the assembly yet report
    # no installed SAPI voices on the same machine.
    powershell = resolve_executable("powershell.exe", "powershell")
    path.parent.mkdir(parents=True, exist_ok=True)
    script = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "Add-Type -AssemblyName System.Speech",
            "$speech = [System.Speech.Synthesis.SpeechSynthesizer]::new()",
            "try {",
            "  $speech.Rate = 0",
            f"  $speech.SetOutputToWaveFile({powershell_literal(path.resolve())})",
            f"  $speech.Speak({powershell_literal(text)})",
            "} finally {",
            "  $speech.Dispose()",
            "}",
        ]
    )
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded_powershell_command(script),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        creationflags=_creation_flags(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise GoldenError(f"System.Speech synthesis failed: {detail[-1000:]}")
    if not path.is_file() or path.stat().st_size <= 44:
        raise GoldenError("System.Speech did not create a valid WAV fixture")


def create_fixture_image(ffmpeg: str, path: Path) -> None:
    binary = resolve_executable(ffmpeg, "ffmpeg")
    path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x17324d:s=1280x720:d=1",
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-update",
            "1",
            str(path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        creationflags=_creation_flags(),
    )
    if completed.returncode != 0 or not path.is_file() or path.stat().st_size <= 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise GoldenError(f"FFmpeg fixture image generation failed: {detail[-1000:]}")


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers or {},
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout if timeout is None else timeout
            ) as response:
                payload = response.read()
                response_headers = {
                    str(key).lower(): str(value)
                    for key, value in response.headers.items()
                }
                return payload, response_headers
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            raise GoldenError(
                f"{method.upper()} {path} returned HTTP {exc.code}: {detail[-2000:]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GoldenError(f"{method.upper()} {path} failed: {exc}") from exc

    def json(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[Any, dict[str, str]]:
        request_headers = dict(headers or {})
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        raw, response_headers = self._request(
            method, path, body=body, headers=request_headers, timeout=timeout
        )
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoldenError(f"{method.upper()} {path} returned invalid JSON") from exc
        return decoded, response_headers

    def post_sse(
        self, path: str, *, operation: str, timeout: float
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        raw, _ = self._request("POST", path, body=b"", timeout=timeout)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GoldenError(f"{operation} returned non-UTF-8 SSE") from exc
        events = parse_sse_text(text)
        done = require_successful_sse(events, operation)
        return events, done

    def upload(self, path: str, source: Path) -> Any:
        boundary = "pipelinegolden" + uuid.uuid4().hex
        body = multipart_body(
            field="file",
            filename=source.name,
            content_type="audio/wav",
            content=source.read_bytes(),
            boundary=boundary,
        )
        raw, _ = self._request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=120,
        )
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoldenError("Audio upload returned invalid JSON") from exc

    def download(self, path: str, destination: Path) -> dict[str, Any]:
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        request = urllib.request.Request(url, method="GET")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            dir=str(destination.parent), prefix=destination.name + ".", suffix=".tmp"
        )
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as output:
                try:
                    response_context = urllib.request.urlopen(request, timeout=300)
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")
                    raise GoldenError(
                        f"GET {path} returned HTTP {exc.code}: {detail[-2000:]}"
                    ) from exc
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    raise GoldenError(f"GET {path} failed: {exc}") from exc
                with response_context as response:
                    headers = {
                        str(key).lower(): str(value)
                        for key, value in response.headers.items()
                    }
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            os.replace(temporary_name, destination)
        finally:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        return {"path": str(destination), "sha256": digest.hexdigest(), "size": size, "headers": headers}


class _OfflineLmHandler(BaseHTTPRequestHandler):
    """Fail every LM request immediately so production fallbacks are stable."""

    protocol_version = "HTTP/1.1"

    def _reject(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length:
            self.rfile.read(length)
        body = b'{"error":"offline golden fallback"}'
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reject
    do_POST = _reject
    do_PUT = _reject
    do_DELETE = _reject

    def log_message(self, _format: str, *_args: Any) -> None:
        return


@dataclass
class OfflineLmStub:
    server: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self.server is None:
            raise GoldenError("Offline LM stub is not running")
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def start(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OfflineLmHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="pipeline-golden-offline-lm",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


def _tail(path: Path, limit: int = 6000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


@dataclass
class IsolatedServer:
    run_dir: Path
    settings_path: Path
    port: int
    process: subprocess.Popen[bytes] | None = None
    _stdout: Any = None
    _stderr: Any = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def stdout_path(self) -> Path:
        return self.run_dir / "server.stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self.run_dir / "server.stderr.log"

    def start(self, timeout: float) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "PIPELINE_SETTINGS_PATH": str(self.settings_path.resolve()),
                "PIPELINE_JOBS_DB": str((self.run_dir / "jobs.sqlite3").resolve()),
                "PIPELINE_INSTANCE_LOCK": str((self.run_dir / "server.lock").resolve()),
                "PYTHONUTF8": "1",
            }
        )
        self._stdout = self.stdout_path.open("wb")
        self._stderr = self.stderr_path.open("wb")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=self._stdout,
            stderr=self._stderr,
            creationflags=_creation_flags(),
        )
        deadline = time.monotonic() + timeout
        client = ApiClient(self.base_url, timeout=1)
        last_error = ""
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self._flush_logs()
                raise GoldenError(
                    "Isolated server exited during startup.\n"
                    + _tail(self.stderr_path)
                    + _tail(self.stdout_path)
                )
            try:
                payload, _ = client.json("GET", "/api/health", timeout=1)
                if isinstance(payload, dict) and payload.get("status") == "ok":
                    return
            except GoldenError as exc:
                last_error = str(exc)
            time.sleep(0.2)
        raise GoldenError(f"Isolated server did not become healthy: {last_error}")

    def _flush_logs(self) -> None:
        for stream in (self._stdout, self._stderr):
            if stream is not None:
                try:
                    stream.flush()
                except OSError:
                    pass

    def stop(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        self._flush_logs()
        for stream in (self._stdout, self._stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _inside(child: Path, root: Path) -> bool:
    try:
        resolved_child = child.resolve()
        resolved_root = root.resolve()
    except (OSError, ValueError):
        return False
    return resolved_child == resolved_root or resolved_root in resolved_child.parents


def project_relative_path(project_dir: Path, value: Any, label: str) -> Path:
    raw = str(value or "").strip()
    require(bool(raw), f"{label} path is empty")
    relative = Path(raw)
    require(not relative.is_absolute() and ".." not in relative.parts, f"{label} path is unsafe")
    candidate = (project_dir / relative).resolve()
    require(_inside(candidate, project_dir), f"{label} path escapes the project")
    return candidate


def validate_manifest_contract(
    manifest: dict[str, Any],
    info: dict[str, Any],
    *,
    sidecar_sha256: str,
    sidecar_size: int,
    output_sha256: str,
    output_size: int,
) -> dict[str, Any]:
    """Pure structural/hash contract shared by the harness and unit tests."""
    require(
        manifest.get("manifest_type") == "pipeline-studio.master-export",
        "Unexpected Master manifest type",
    )
    manifest_id = str(manifest.get("manifest_id", "") or "")
    require(bool(manifest_id), "Master manifest_id is empty")
    require(manifest_id == str(info.get("manifest_id", "") or ""), "manifest_id mismatch")
    require(is_sha256(sidecar_sha256), "Downloaded sidecar SHA-256 is malformed")
    require(sidecar_sha256 == str(info.get("sha256", "") or "").lower(), "Sidecar SHA-256 mismatch")
    require(sidecar_size == int(info.get("size", -1)), "Sidecar size mismatch")

    export = manifest.get("export")
    require(isinstance(export, dict), "Master manifest export contract is missing")
    require(export.get("mode") == "master", "Export mode is not master")
    profile = export.get("profile")
    require(isinstance(profile, dict) and profile.get("name") == "delivery", "Export profile is not delivery")
    technical_qc = export.get("technical_qc")
    require(isinstance(technical_qc, dict) and technical_qc.get("ok") is True, "Master technical QC failed")
    require(output_sha256 == str(export.get("sha256", "") or "").lower(), "MP4 SHA-256 mismatch")
    require(output_sha256 == str(info.get("output_sha256", "") or "").lower(), "Master info output SHA-256 mismatch")
    require(output_size == int(export.get("size", -1)), "MP4 size mismatch")

    settings = export.get("settings")
    require(isinstance(settings, dict), "Export settings contract is missing")
    require(
        json_content_hash(settings) == str(export.get("settings_fingerprint", "") or ""),
        "Settings fingerprint mismatch",
    )
    edit_contract = export.get("edit_contract")
    require(isinstance(edit_contract, dict), "Edit contract is missing")
    provenance = manifest.get("provenance")
    require(isinstance(provenance, dict), "Provenance contract is missing")
    cards = provenance.get("cards")
    require(isinstance(cards, list) and len(cards) == 1, "Golden Master must contain exactly one provenance card")
    contract_fingerprint = str(export.get("contract_fingerprint", "") or "").lower()
    require(is_sha256(contract_fingerprint), "Contract fingerprint is malformed")
    require(
        contract_fingerprint
        == json_content_hash(
            {
                "profile": profile,
                "settings": settings,
                "edit_contract": edit_contract,
                "provenance": cards,
            }
        ),
        "Contract fingerprint mismatch",
    )

    card = cards[0]
    require(isinstance(card, dict), "Provenance card is malformed")
    variant = card.get("variant")
    require(isinstance(variant, dict), "Selected provenance variant is missing")
    media_hash = str(card.get("media_sha256", "") or "").lower()
    variant_hash = str(variant.get("file_sha256", "") or "").lower()
    require(is_sha256(media_hash), "Provenance media SHA-256 is malformed")
    require(media_hash == variant_hash, "Variant and selected media hashes differ")
    require(variant.get("source") == "manual", "Golden variant source is not manual")
    require(card.get("output_field") == "assigned_media", "Golden media was not selected through assigned_media")
    return {
        "manifest_id": manifest_id,
        "contract_fingerprint": contract_fingerprint,
        "media_sha256": media_hash,
        "profile": profile.get("name"),
        "technical_qc": technical_qc,
    }


def wait_for_jobs(
    client: ApiClient,
    project_id: str,
    *,
    timeout: float = 15,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload, _ = client.json("GET", f"/api/projects/{project_id}/jobs?limit=50")
        if isinstance(payload, dict):
            latest = payload
            items = payload.get("items")
            if (
                not payload.get("running")
                and isinstance(items, list)
                and len(items) >= sum(EXPECTED_JOB_RESOURCES.values())
                and all(item.get("status") != "running" for item in items if isinstance(item, dict))
            ):
                return payload
        time.sleep(0.1)
    raise GoldenError(f"Durable jobs did not settle: {latest}")


def validate_jobs(payload: dict[str, Any], project_id: str) -> list[dict[str, Any]]:
    require(not payload.get("running"), "Project still reports a running job")
    items = payload.get("items")
    require(isinstance(items, list), "Job history is malformed")
    records = [item for item in items if isinstance(item, dict)]
    require(len(records) == sum(EXPECTED_JOB_RESOURCES.values()), "Golden run must produce exactly four durable jobs")
    require(all(item.get("project_id") == project_id for item in records), "Job history contains another project")
    bad = [item for item in records if item.get("status") != "succeeded"]
    require(not bad, f"Golden job did not succeed: {bad}")
    resources = Counter(str(item.get("resource", "") or "") for item in records)
    require(resources == EXPECTED_JOB_RESOURCES, f"Unexpected golden job resources: {dict(resources)}")
    return records


def _get_board(client: ApiClient, project_id: str) -> tuple[list[dict[str, Any]], str]:
    payload, headers = client.json("GET", f"/api/projects/{project_id}/asset-board")
    require(isinstance(payload, list) and bool(payload), "Asset Board is empty")
    require(all(isinstance(card, dict) for card in payload), "Asset Board is malformed")
    etag = str(headers.get("etag", "") or "")
    require(bool(etag), "Asset Board response is missing ETag")
    return payload, etag


def _save_events(run_dir: Path, name: str, events: Iterable[dict[str, Any]]) -> None:
    atomic_write_json(run_dir / f"{name}.sse.json", list(events))


def _verify_project_artifacts(
    project_dir: Path,
    manifest: dict[str, Any],
    info: dict[str, Any],
    downloaded_manifest: Path,
    downloaded_output: Path,
) -> dict[str, Any]:
    sidecar_path = project_relative_path(project_dir / "exports", info.get("path"), "Master sidecar")
    export_contract = manifest["export"]
    output_path = project_relative_path(
        project_dir / "exports", export_contract.get("path"), "Master output"
    )
    require(sidecar_path.is_file(), "Published Master sidecar is missing")
    require(output_path.is_file(), "Published Master output is missing")
    require(
        sha256_file(sidecar_path) == sha256_file(downloaded_manifest),
        "Downloaded sidecar differs from project sidecar",
    )
    require(sha256_file(output_path) == sha256_file(downloaded_output), "Downloaded MP4 differs from project MP4")

    provenance = manifest["provenance"]
    variant_manifest = provenance.get("variant_manifest")
    require(isinstance(variant_manifest, dict), "Variant manifest contract is missing")
    variant_manifest_path = project_relative_path(
        project_dir, variant_manifest.get("path"), "Variant manifest"
    )
    require(variant_manifest_path.is_file(), "Variant manifest file is missing")
    require(
        sha256_file(variant_manifest_path)
        == str(variant_manifest.get("sha256", "") or "").lower(),
        "Variant manifest SHA-256 mismatch",
    )

    card = provenance["cards"][0]
    media_path = project_relative_path(project_dir, card.get("media_path"), "Selected media")
    variant_path = project_relative_path(
        project_dir, card["variant"].get("file_path"), "Variant media"
    )
    require(media_path.is_file() and variant_path.is_file(), "Provenance media file is missing")
    require(sha256_file(media_path) == card["media_sha256"], "Selected media bytes changed")
    require(sha256_file(variant_path) == card["variant"]["file_sha256"], "Immutable variant bytes changed")
    require(_inside(variant_path, project_dir / "assets" / "variants"), "Variant is outside immutable storage")

    # Use the production verifier for the append-only chain. Importing this
    # module is read-only and does not initialise the job registry.
    from core.export_manifest import load_master_export_index

    records = load_master_export_index(project_dir / "exports")
    require(len(records) == 1, "Fresh golden project must contain one Master index record")
    record = records[0]
    require(record.get("manifest_id") == info.get("manifest_id"), "Master index manifest_id mismatch")
    require(record.get("sidecar_sha256") == sha256_file(sidecar_path), "Master index sidecar hash mismatch")
    require(record.get("output_sha256") == sha256_file(output_path), "Master index output hash mismatch")
    return {
        "sidecar": str(sidecar_path),
        "output": str(output_path),
        "variant_manifest": str(variant_manifest_path),
        "variant": str(variant_path),
        "master_index_records": len(records),
    }


def run_golden(
    *,
    run_dir: Path,
    source_settings_path: Path,
    port: int,
    server_timeout: float,
    job_timeout: float,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=False)
    projects_dir = run_dir / "projects"
    projects_dir.mkdir()
    source_settings = load_source_settings(source_settings_path)
    isolated_settings = build_isolated_settings(source_settings, projects_dir)
    settings_path = run_dir / "settings.json"
    offline_lm = OfflineLmStub()
    offline_lm.start()
    isolated_settings["lm_url"] = offline_lm.base_url
    server = IsolatedServer(run_dir, settings_path, port)
    started_at = utc_now()
    client: ApiClient | None = None
    project_id = ""
    try:
        atomic_write_json(settings_path, isolated_settings)
        server.start(server_timeout)
        client = ApiClient(server.base_url, timeout=60)
        for target in ("ffmpeg", "whisper"):
            diagnostic, _ = client.json("GET", f"/api/diagnostics/{target}")
            require(
                isinstance(diagnostic, dict) and diagnostic.get("ok") is True,
                f"Required diagnostic failed ({target}): {diagnostic}",
            )

        project_name = "Golden E2E " + uuid.uuid4().hex[:10]
        project, _ = client.json(
            "POST",
            "/api/projects",
            {"name": project_name, "preset": "tutorial", "language": "en"},
        )
        require(isinstance(project, dict), "Project creation response is malformed")
        project_dir = Path(str(project.get("dir", "") or "")).resolve()
        require(_inside(project_dir, projects_dir), "Golden project escaped isolated output_base")
        require(project_dir.is_dir(), "Golden project directory was not created")
        project_id = project_dir.name

        client.json(
            "PUT",
            f"/api/projects/{project_id}/settings",
            {
                "shot_mode": "ai",
                "route_priority": "economy",
                "seg_min_seconds": 1,
                "seg_max_seconds": 30,
                "seg_target_scenes": 1,
            },
        )

        speech_path = run_dir / "golden_voice.wav"
        synthesise_speech(speech_path)
        upload = client.upload(f"/api/projects/{project_id}/upload-audio", speech_path)
        require(
            isinstance(upload, dict) and upload.get("filename") == speech_path.name,
            "Audio upload contract failed",
        )

        for evidence_name, endpoint, label in (
            ("transcription", f"/api/projects/{project_id}/transcribe", "Transcription"),
            ("scenes", f"/api/projects/{project_id}/analyze-scenes", "Scene analysis"),
            ("shots", f"/api/projects/{project_id}/generate-shots", "Shot generation"),
        ):
            events, _ = client.post_sse(endpoint, operation=label, timeout=job_timeout)
            _save_events(run_dir, evidence_name, events)

        cards, _ = _get_board(client, project_id)
        fixture_image = project_dir / "assets" / "images" / "golden_fixture.png"
        create_fixture_image(isolated_settings["ffmpeg"], fixture_image)

        # Re-read before every mutation: each successful PUT advances the ETag.
        for card_index in range(len(cards)):
            current_cards, etag = _get_board(client, project_id)
            require(card_index < len(current_cards), "Asset Board changed during golden mutation")
            if card_index == 0:
                patch = {
                    "assigned_media": str(fixture_image.resolve()),
                    "route": "image",
                    "visual_type": "ai_image",
                    "status": "generated",
                    "approved": True,
                    "ltx_draft": False,
                    "text_overlay_enabled": False,
                }
            else:
                patch = {"status": "rejected", "approved": False, "ltx_draft": False}
            client.json(
                "PUT",
                f"/api/projects/{project_id}/asset-board/{card_index}",
                patch,
                headers={"If-Match": etag},
            )

        variants, _ = client.json(
            "GET", f"/api/projects/{project_id}/asset-board/0/variants"
        )
        require(isinstance(variants, dict), "Asset variant response is malformed")
        selected_id = (variants.get("selections") or {}).get("assigned_media")
        recorded = variants.get("variants")
        require(bool(selected_id) and isinstance(recorded, list), "Manual assigned-media variant was not selected")
        selected = next(
            (item for item in recorded if isinstance(item, dict) and item.get("id") == selected_id),
            None,
        )
        require(
            isinstance(selected, dict) and selected.get("source") == "manual",
            "Selected variant is not an immutable manual snapshot",
        )

        client.json(
            "PUT",
            f"/api/projects/{project_id}/timeline-settings",
            {"voice_start": "00:00:00"},
        )
        validation, _ = client.json("POST", f"/api/projects/{project_id}/validate")
        errors = (((validation or {}).get("summary") or {}).get("counts") or {}).get("error")
        require(errors == 0, f"Golden validation contains errors: {validation}")
        atomic_write_json(run_dir / "validation.json", validation)

        export_events, export_done = client.post_sse(
            f"/api/projects/{project_id}/export?mode=master&profile=delivery",
            operation="Master export",
            timeout=job_timeout,
        )
        _save_events(run_dir, "export", export_events)
        gates = [event.get("data") for event in export_events if event.get("event") == "gate"]
        require(
            bool(gates)
            and isinstance(gates[-1], dict)
            and gates[-1].get("allowed") is True,
            "Master gate was not allowed",
        )
        require(
            isinstance(export_done.get("technical_qc"), dict)
            and export_done["technical_qc"].get("ok") is True,
            "Master SSE did not report successful technical QC",
        )

        state, _ = client.json("GET", f"/api/projects/{project_id}/state")
        require(isinstance(state, dict), "Project state response is malformed")
        stage_states = state.get("stage_states")
        require(isinstance(stage_states, dict), "Project stage state is missing")
        stage_failures = {
            stage: (stage_states.get(stage) or {}).get("status")
            for stage in REQUIRED_STAGES
            if (stage_states.get(stage) or {}).get("status") != "completed"
        }
        require(not stage_failures, f"Golden stages are not current: {stage_failures}")

        jobs_payload = wait_for_jobs(client, project_id)
        jobs = validate_jobs(jobs_payload, project_id)
        atomic_write_json(run_dir / "jobs.json", jobs_payload)

        info, _ = client.json("GET", f"/api/projects/{project_id}/master-manifest")
        require(isinstance(info, dict), "Master manifest info is malformed")
        manifest_download = client.download(
            f"/api/projects/{project_id}/download-manifest",
            run_dir / "downloaded.master.manifest.json",
        )
        output_download = client.download(
            f"/api/projects/{project_id}/download",
            run_dir / "downloaded.master.mp4",
        )
        try:
            manifest = json.loads(
                Path(manifest_download["path"]).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise GoldenError("Downloaded Master sidecar is invalid JSON") from exc
        require(isinstance(manifest, dict), "Downloaded Master sidecar is malformed")

        manifest_summary = validate_manifest_contract(
            manifest,
            info,
            sidecar_sha256=manifest_download["sha256"],
            sidecar_size=manifest_download["size"],
            output_sha256=output_download["sha256"],
            output_size=output_download["size"],
        )
        manifest_headers = manifest_download.get("headers") or {}
        require(
            str(manifest_headers.get("x-content-sha256", "") or "").lower()
            == manifest_download["sha256"],
            "Manifest download X-Content-SHA256 mismatch",
        )
        require(
            str(manifest_headers.get("x-manifest-id", "") or "")
            == manifest_summary["manifest_id"],
            "Manifest download X-Manifest-ID mismatch",
        )
        artifact_summary = _verify_project_artifacts(
            project_dir,
            manifest,
            info,
            Path(manifest_download["path"]),
            Path(output_download["path"]),
        )

        return {
            "schema_version": 1,
            "ok": True,
            "started_at": started_at,
            "completed_at": utc_now(),
            "run_dir": str(run_dir.resolve()),
            "project_id": project_id,
            "project_dir": str(project_dir),
            "settings_path": str(settings_path.resolve()),
            "server": {"base_url": server.base_url, "port": server.port},
            "stages": {stage: stage_states[stage]["status"] for stage in REQUIRED_STAGES},
            "jobs": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "resource": item.get("resource"),
                    "status": item.get("status"),
                }
                for item in jobs
            ],
            "validation": validation.get("summary"),
            "manifest": manifest_summary,
            "downloads": {
                "manifest": {
                    key: value
                    for key, value in manifest_download.items()
                    if key != "headers"
                },
                "output": {
                    key: value
                    for key, value in output_download.items()
                    if key != "headers"
                },
            },
            "artifacts": artifact_summary,
        }
    finally:
        # If a timeout/assertion interrupts a live worker, request cooperative
        # cancellation before stopping Uvicorn.  Whisper/FFmpeg poll that event
        # and terminate their own subprocesses, avoiding an orphaned tool.
        if client is not None and project_id:
            try:
                jobs_before, _ = client.json(
                    "GET", f"/api/projects/{project_id}/jobs?limit=50", timeout=2
                )
                if isinstance(jobs_before, dict) and jobs_before.get("running"):
                    client.json(
                        "POST", f"/api/projects/{project_id}/cancel", timeout=2
                    )
                    cancel_deadline = time.monotonic() + 10
                    while time.monotonic() < cancel_deadline:
                        current, _ = client.json(
                            "GET",
                            f"/api/projects/{project_id}/jobs?limit=50",
                            timeout=2,
                        )
                        if isinstance(current, dict) and not current.get("running"):
                            break
                        time.sleep(0.1)
            except Exception:
                pass
        try:
            server.stop()
        finally:
            offline_lm.stop()


def _new_run_dir(configured: Path | None) -> Path:
    if configured is None:
        evidence_root = ROOT / ".pytest-tmp"
        evidence_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return evidence_root / f"golden-{stamp}-{uuid.uuid4().hex[:8]}"
    path = configured.expanduser().resolve()
    if path.exists():
        raise GoldenError(f"Run directory already exists: {path}")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-settings",
        type=Path,
        default=DEFAULT_SOURCE_SETTINGS,
        help="Read-only local settings source for FFmpeg and Whisper paths.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        help=(
            "New directory for isolated evidence; defaults to .pytest-tmp "
            "under the repository."
        ),
    )
    parser.add_argument("--port", type=int, default=0, help="Local port; 0 chooses a free port.")
    parser.add_argument("--server-timeout", type=float, default=30.0)
    parser.add_argument("--job-timeout", type=float, default=900.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    run_dir: Path | None = None
    try:
        if args.port and not 1 <= args.port <= 65535:
            raise GoldenError("--port must be 0 or between 1 and 65535")
        if args.server_timeout <= 0 or args.job_timeout <= 0:
            raise GoldenError("Timeouts must be positive")
        run_dir = _new_run_dir(args.run_dir)
        result = run_golden(
            run_dir=run_dir,
            source_settings_path=args.source_settings.expanduser().resolve(),
            port=args.port or find_free_port(),
            server_timeout=args.server_timeout,
            job_timeout=args.job_timeout,
        )
        atomic_write_json(run_dir / "golden-report.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "ok": False,
            "failed_at": utc_now(),
            "run_dir": str(run_dir.resolve()) if run_dir is not None else "",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        }
        if run_dir is not None:
            try:
                run_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_json(run_dir / "golden-failure.json", failure)
            except OSError:
                pass
        print(json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
