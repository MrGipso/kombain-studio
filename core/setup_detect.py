"""First-run discovery of external tools for the setup wizard.

Everything here is read-only: it inspects ``PATH`` and a short list of
well-known install folders, and probes the local LM Studio / ComfyUI ports.
Nothing is executed and nothing is written, so the wizard can call it freely
before the user has confirmed any path.

Windows is the primary target (the shipped ``.bat`` launchers, the historic
``wishper/`` folder next to the project), Linux is supported for development.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from core.config import ROOT, Settings

DEFAULT_LM_URL = "http://127.0.0.1:1234/v1"
DEFAULT_COMFY_URL = "http://127.0.0.1:8188"

# Preference order for whisper ggml models: better quality first. Turbo is
# large-v3 distilled and is the recommended default when present.
_MODEL_PREFERENCE = (
    "ggml-large-v3-turbo",
    "ggml-large",
    "ggml-medium",
    "ggml-small",
    "ggml-base",
    "ggml-tiny",
)
# whisper.cpp ships VAD models with the same ggml-*.bin naming; they are not
# transcription models and must never be offered as one.
_NON_TRANSCRIPTION_MARKERS = ("silero", "vad")


@dataclass
class ToolCandidates:
    """Discovered values for one setting, best candidate first."""

    current: str = ""
    current_ok: bool = False
    candidates: list[str] = field(default_factory=list)


@dataclass
class ServiceProbe:
    url: str
    reachable: bool = False
    models: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class SetupDetection:
    platform: str
    project_root: str
    output_base: ToolCandidates
    ffmpeg: ToolCandidates
    whisper_cli: ToolCandidates
    whisper_model: ToolCandidates
    omnivoice_cli: ToolCandidates
    # Configured URL first, then the default localhost URL when different.
    lm: list[ServiceProbe]
    comfy: list[ServiceProbe]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_windows(platform: str) -> bool:
    return platform.startswith("win")


def _exe(name: str, platform: str) -> str:
    return f"{name}.exe" if _is_windows(platform) else name


def _env_path(name: str, environ: dict[str, str]) -> Path | None:
    value = environ.get(name, "").strip()
    return Path(value) if value else None


def _unique_existing(paths: Iterable[Path | None], *, want_dir: bool = False) -> list[str]:
    seen: set[str] = set()
    found: list[str] = []
    for path in paths:
        if path is None:
            continue
        try:
            ok = path.is_dir() if want_dir else path.is_file()
        except OSError:
            ok = False
        if not ok:
            continue
        # Dedupe on the real file but report the path as found: a symlink such
        # as /usr/bin/ffmpeg or a WinGet shim keeps the basename that the
        # settings route validates, while its target may not.
        try:
            key = str(path.resolve()).casefold()
        except OSError:
            key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            found.append(str(path.absolute()))
    return found


def _which(name: str, which: Callable[[str], str | None]) -> Path | None:
    hit = which(name)
    return Path(hit) if hit else None


def _ffmpeg_paths(platform: str, environ: dict[str, str], home: Path, which) -> list[Path | None]:
    paths: list[Path | None] = [_which("ffmpeg", which)]
    paths.append(ROOT / "tools" / "ffmpeg" / _exe("ffmpeg", platform))
    paths.append(ROOT / "tools" / "ffmpeg" / "bin" / _exe("ffmpeg", platform))
    if _is_windows(platform):
        program_files = _env_path("ProgramFiles", environ)
        local = _env_path("LOCALAPPDATA", environ)
        paths += [
            Path("C:/ffmpeg/bin/ffmpeg.exe"),
            program_files / "ffmpeg" / "bin" / "ffmpeg.exe" if program_files else None,
            local / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe" if local else None,
            home / "scoop" / "shims" / "ffmpeg.exe",
            Path("C:/ProgramData/chocolatey/bin/ffmpeg.exe"),
        ]
    else:
        paths += [Path("/usr/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg"), home / ".local" / "bin" / "ffmpeg"]
    return paths


def _whisper_roots(platform: str, home: Path) -> list[Path]:
    """Folders that may hold a whisper.cpp checkout or a prebuilt release."""
    return [
        ROOT / "tools" / "whisper.cpp",
        ROOT / "wishper",  # historic name of the bundled Windows release
        ROOT / "whisper",
        ROOT.parent / "whisper.cpp",
        home / "src" / "whisper.cpp",
        home / "whisper.cpp",
    ] + ([Path("C:/whisper.cpp"), Path("C:/whisper")] if _is_windows(platform) else [])


def _whisper_cli_paths(platform: str, home: Path, which) -> list[Path | None]:
    paths: list[Path | None] = [_which("whisper-cli", which)]
    for root in _whisper_roots(platform, home):
        for sub in ("", "build/bin", "build/bin/Release", "Release", "bin"):
            base = root / sub if sub else root
            paths.append(base / _exe("whisper-cli", platform))
    return paths


def _whisper_model_rank(path: Path) -> tuple[int, str]:
    name = path.name.lower()
    for index, prefix in enumerate(_MODEL_PREFERENCE):
        if name.startswith(prefix):
            return index, name
    return len(_MODEL_PREFERENCE), name


def _whisper_model_paths(platform: str, home: Path, cli_candidates: list[str]) -> list[Path]:
    dirs = [ROOT / "models"]
    for root in _whisper_roots(platform, home):
        dirs += [root / "models", root]
    # A found CLI usually sits in <checkout>/build/bin; its models live in
    # <checkout>/models.
    for cli in cli_candidates:
        parent = Path(cli).parent
        dirs += [parent / "models", parent.parent / "models", parent.parent.parent / "models"]
    models: list[Path] = []
    for directory in dirs:
        try:
            if not directory.is_dir():
                continue
            entries = list(directory.glob("ggml-*.bin"))
        except OSError:
            continue
        for entry in entries:
            lowered = entry.name.lower()
            if any(marker in lowered for marker in _NON_TRANSCRIPTION_MARKERS):
                continue
            models.append(entry)
    return sorted(models, key=_whisper_model_rank)


def _omnivoice_paths(platform: str, environ: dict[str, str], home: Path, which) -> list[Path | None]:
    name = _exe("omnivoice-infer", platform)
    paths: list[Path | None] = [_which("omnivoice-infer", which), home / ".local" / "bin" / name]
    if _is_windows(platform):
        appdata = _env_path("APPDATA", environ)
        if appdata:
            paths += sorted((appdata / "Python").glob(f"Python3*/Scripts/{name}"))
    return paths


def _path_ok(value: str, *, want_dir: bool = False) -> bool:
    if not value:
        return False
    try:
        path = Path(value).expanduser()
        return path.is_dir() if want_dir else path.is_file()
    except OSError:
        return False


def _tool(current: str, found: list[str], *, want_dir: bool = False) -> ToolCandidates:
    current = (current or "").strip()
    return ToolCandidates(current=current, current_ok=_path_ok(current, want_dir=want_dir), candidates=found)


def _get_json(url: str, timeout: float) -> tuple[bool, Any, str]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            body = resp.read(200_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return True, None, f"HTTP {exc.code}"
    except Exception as exc:  # connection refused, timeout, DNS…
        return False, None, str(exc)
    try:
        return True, json.loads(body), ""
    except json.JSONDecodeError:
        return True, None, "ответ не JSON"


def probe_lm(url: str, *, provider: str = "lmstudio", timeout: float = 2.0, getter=_get_json) -> ServiceProbe:
    base = (url or DEFAULT_LM_URL).rstrip("/")
    probe = ServiceProbe(url=base)
    if provider != "lmstudio":
        # Cloud endpoints need the API key; the wizard checks them through the
        # regular diagnostics route after the key is saved.
        probe.detail = "облачный API — проверка после сохранения ключа"
        return probe
    ok, payload, detail = getter(f"{base}/models", timeout)
    probe.reachable = ok
    probe.detail = detail
    if ok and isinstance(payload, dict):
        probe.models = [
            str(item.get("id"))
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]
    return probe


def probe_comfy(url: str, *, timeout: float = 2.0, getter=_get_json) -> ServiceProbe:
    base = (url or DEFAULT_COMFY_URL).rstrip("/")
    probe = ServiceProbe(url=base)
    ok, payload, detail = getter(f"{base}/system_stats", timeout)
    probe.reachable = ok
    probe.detail = detail
    if ok and isinstance(payload, dict):
        devices = payload.get("devices") or []
        probe.models = [str(d.get("name")) for d in devices if isinstance(d, dict) and d.get("name")]
    return probe


def _probes(configured: str, default: str, probe: Callable[[str], ServiceProbe]) -> list[ServiceProbe]:
    """Probe the configured URL and, if different, the stock localhost one."""
    urls = [(configured or default).rstrip("/")]
    if default.rstrip("/") not in urls:
        urls.append(default.rstrip("/"))
    return [probe(url) for url in urls]


def detect(
    settings: Settings,
    *,
    platform: str | None = None,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    probe_services: bool = True,
) -> SetupDetection:
    platform = platform or sys.platform
    environ = dict(os.environ if environ is None else environ)
    home = home or Path.home()

    ffmpeg_found = _unique_existing(_ffmpeg_paths(platform, environ, home, which))
    cli_found = _unique_existing(_whisper_cli_paths(platform, home, which))
    model_found = _unique_existing(_whisper_model_paths(platform, home, cli_found))
    omni_found = _unique_existing(_omnivoice_paths(platform, environ, home, which))

    default_projects = ROOT / "projects"
    output_candidates = [str(default_projects.resolve())]

    provider = settings.lm_provider or "lmstudio"
    if probe_services:
        lm = _probes(settings.lm_url, DEFAULT_LM_URL, lambda url: probe_lm(url, provider=provider))
        if provider != "lmstudio":
            lm = lm[:1]  # never fall back to a local LM Studio for a cloud setup
        comfy = _probes(settings.comfy_url, DEFAULT_COMFY_URL, probe_comfy)
    else:
        lm, comfy = [], []

    omni_current = (settings.omnivoice_cli or "").strip()
    omni = _tool(omni_current, omni_found)
    if omni_current and not omni.current_ok and Path(omni_current).name == omni_current:
        # A bare command name (the default) is fine when it resolves on PATH.
        omni.current_ok = which(omni_current) is not None

    return SetupDetection(
        platform="windows" if _is_windows(platform) else ("macos" if platform == "darwin" else "linux"),
        project_root=str(ROOT),
        output_base=_tool(settings.output_base, output_candidates, want_dir=True),
        ffmpeg=_tool(settings.ffmpeg, ffmpeg_found),
        whisper_cli=_tool(settings.whisper_cli, cli_found),
        whisper_model=_tool(settings.whisper_model, model_found),
        omnivoice_cli=omni,
        lm=lm,
        comfy=comfy,
    )
