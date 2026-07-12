"""Passive local preflight with an explicit opt-in active inference mode."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.routers import diagnostics
from core.config import load_settings


TARGETS = ("ffmpeg", "whisper", "comfy", "lm", "embed", "woosh")


def _checkers(settings, *, active: bool = False) -> dict[str, Callable[[], object]]:
    return {
        "ffmpeg": lambda: diagnostics._check_binary(
            "ffmpeg", settings.ffmpeg, "ffmpeg"
        ),
        "whisper": lambda: diagnostics._check_whisper(settings),
        "comfy": lambda: diagnostics._check_comfy(settings),
        "lm": lambda: (
            diagnostics._check_lm_inference(settings)
            if active else diagnostics._check_lm(settings)
        ),
        "embed": lambda: (
            diagnostics._check_embed(settings)
            if active else diagnostics._check_embed_available(settings)
        ),
        "woosh": lambda: diagnostics._check_woosh(settings),
    }


def run_checks(
    settings,
    targets: list[str] | tuple[str, ...],
    *,
    checkers: dict[str, Callable[[], object]] | None = None,
) -> dict:
    selected = list(dict.fromkeys(targets))
    unknown = sorted(set(selected) - set(TARGETS))
    if unknown:
        raise ValueError(f"Unknown integration targets: {', '.join(unknown)}")
    available = checkers or _checkers(settings)
    with ThreadPoolExecutor(max_workers=max(1, len(selected))) as executor:
        futures = {name: executor.submit(available[name]) for name in selected}
        results = {}
        for name, future in futures.items():
            try:
                result = future.result()
                if hasattr(result, "model_dump"):
                    results[name] = result.model_dump(mode="json")
                elif isinstance(result, dict):
                    results[name] = dict(result)
                else:
                    raise TypeError(f"Unsupported result type: {type(result).__name__}")
            except Exception as exc:
                results[name] = {
                    "target": name,
                    "ok": False,
                    "message": "Diagnostic crashed",
                    "detail": str(exc),
                }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "results": results,
    }


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    # Windows consoles otherwise inherit a legacy code page while our report is
    # JSON/UTF-8.  Explicit UTF-8 keeps Russian diagnostic messages readable in
    # terminals and CI logs; redirected files are written separately/atomically.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", nargs="+", choices=TARGETS, default=list(TARGETS))
    parser.add_argument(
        "--require",
        nargs="*",
        choices=TARGETS,
        default=[],
        help="Return a non-zero exit code if any listed target is unavailable.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--active",
        action="store_true",
        help=(
            "Submit tiny chat/embedding requests. This can JIT-load configured "
            "LM models and consume VRAM; passive checks are the default."
        ),
    )
    args = parser.parse_args(argv)
    targets = list(dict.fromkeys([*args.targets, *args.require]))
    settings = load_settings()
    payload = run_checks(
        settings,
        targets,
        checkers=_checkers(settings, active=args.active),
    )
    payload["required"] = list(args.require)
    payload["active"] = bool(args.active)
    if args.output:
        _atomic_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    failed_required = [
        name
        for name in args.require
        if not payload["results"].get(name, {}).get("ok", False)
    ]
    return 1 if failed_required else 0


if __name__ == "__main__":
    raise SystemExit(main())
