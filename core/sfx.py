"""SFX layer (ambient sound per shot).

Generates short ambient/foley beds from each card's `sfx_query` via the Woosh
API server (Sony AI sound-effects model, https://github.com/SonyResearch/Woosh)
and assembles a single project-length "SFX bed" wav that the exporter mixes
under the voice.

Design notes:
- Fully optional: disabled unless `sfx_enabled` is on AND the Woosh server
  responds to /ping. A missing server degrades to "no SFX", never to a failure.
- Generated clips are cached per query hash in <project>/sfx/, so re-exports and
  shots sharing the same ambience (same sfx_query) reuse one file.
- The bed is built with one ffmpeg call: each clip is looped to its shot's
  timeline interval, faded, leveled and delayed to the shot start, then mixed.
- Woosh weights are CC-BY-NC (non-commercial); the caller decided to use it.
"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Callable


def _log(log: Callable[[str], None] | None, msg: str) -> None:
    if log:
        log(msg)


def sfx_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "sfx_enabled", False)) and bool(
        (getattr(settings, "woosh_url", "") or "").strip()
    )


def woosh_ping(woosh_url: str, timeout: int = 5) -> bool:
    """True if the Woosh API server answers /ping."""
    try:
        req = urllib.request.Request(woosh_url.rstrip("/") + "/ping", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return str(data.get("status", "")).lower() == "ok"
    except Exception:
        return False


def generate_sfx(prompt: str, dst: Path, woosh_url: str, timeout: int = 180,
                 seed: int | None = None) -> Path:
    """Generate one SFX clip from `prompt` via Woosh /generate (returns FLAC).

    Uses the distilled Woosh-DFlow model with the parameters from the reference
    client (api/test_api.py). Raises on failure; the caller treats any error as
    "no SFX for this shot".
    """
    payload = {
        "version": "0.1",
        "token": "string",
        "args": {
            "model": "Woosh-DFlow",
            "prompt": prompt,
            "cfg": 3.0,
            "sampler": "heun",
            "num_steps": 5,
            "sigma_min": 0.0001,
            "sigma_max": 80,
            "rho": 7,
            "S_churn": 1,
            "S_min": 0,
            "S_noise": 1,
            "guidance_scale": 7.5,
            "noise_scheduler": "karras",
            "seed": seed if seed is not None else random.randint(0, 2**32 - 1),
        },
    }
    req = urllib.request.Request(
        woosh_url.rstrip("/") + "/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        audio = resp.read()
    if not audio:
        raise RuntimeError("Woosh returned empty audio")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(audio)
    return dst


def _query_hash(query: str) -> str:
    return hashlib.sha1(query.strip().lower().encode("utf-8")).hexdigest()[:12]


def generate_for_query(project_dir: Path, query: str, settings: Any,
                       fresh: bool = False) -> Path:
    """Generate (or fetch the cached) clip for one ambience query.

    fresh=True rolls a new random seed and writes a new file, so "перегенерировать"
    gives a different take while the previous one stays on disk (other cards may
    still reference it). Raises on any failure — the router turns that into a 502.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("Empty sfx query")
    woosh_url = (getattr(settings, "woosh_url", "") or "").strip()
    if not woosh_url:
        raise RuntimeError("Woosh URL is not configured")
    key = _query_hash(query)
    sfx_dir = project_dir / "sfx"
    if fresh:
        import time
        dst = sfx_dir / f"sfx_{key}_{int(time.time())}.flac"
        seed = None  # random take
    else:
        dst = sfx_dir / f"sfx_{key}.flac"
        if dst.exists():
            return dst
        seed = int(key, 16) % (2**32)  # deterministic default take
    generate_sfx(query, dst, woosh_url, seed=seed)
    return dst


def ensure_sfx_for_cards(project_dir: Path, cards: list, settings: Any,
                         log: Callable[[str], None] | None = None) -> bool:
    """Generate missing SFX clips for cards that have an sfx_query.

    Clips are cached by query hash, so N shots in the same location share one
    file. Sets card.sfx_output in place; returns True if any card changed
    (the caller should then persist the board).
    """
    if not sfx_enabled(settings):
        return False
    woosh_url = getattr(settings, "woosh_url", "").strip()
    if not woosh_ping(woosh_url):
        _log(log, f"SFX: Woosh недоступен ({woosh_url}) — слой пропущен.")
        return False

    sfx_dir = project_dir / "sfx"
    changed = False
    generated: dict[str, str] = {}
    for card in cards:
        query = (getattr(card, "sfx_query", "") or "").strip()
        if not query:
            continue
        existing = (getattr(card, "sfx_output", "") or "").strip()
        if existing and Path(existing).exists():
            continue
        key = _query_hash(query)
        if key in generated:
            card.sfx_output = generated[key]
            changed = True
            continue
        dst = sfx_dir / f"sfx_{key}.flac"
        if not dst.exists():
            try:
                _log(log, f"SFX: генерирую '{query}'...")
                # Deterministic seed per query: re-exports reuse the same sound.
                generate_sfx(query, dst, woosh_url,
                             seed=int(key, 16) % (2**32))
            except Exception as exc:
                _log(log, f"SFX: не удалось сгенерировать '{query}': {exc}")
                continue
        generated[key] = str(dst)
        card.sfx_output = str(dst)
        changed = True
    return changed


def build_sfx_bed(cards_with_intervals: list[tuple[Any, tuple[int, int]]],
                  exports_dir: Path, ffmpeg: str, total_end: int,
                  volume_db: float = -24.0,
                  log: Callable[[str], None] | None = None) -> Path | None:
    """Assemble one project-length SFX bed wav from per-card clips.

    `cards_with_intervals` pairs each included card with its tiled [start, end)
    interval (seconds) — the same intervals the video segments use, so sound and
    picture line up. Consecutive shots that share the same sfx file are merged
    into one span so the ambience flows instead of restarting at every cut.
    Returns the bed path, or None when there is nothing to mix.
    """
    # Collapse consecutive same-file shots into continuous spans.
    spans: list[dict] = []  # {src, start, end}
    for card, (start_s, end_s) in cards_with_intervals:
        src = (getattr(card, "sfx_output", "") or "").strip()
        if not src or not Path(src).exists():
            continue
        if spans and spans[-1]["src"] == src and abs(spans[-1]["end"] - start_s) <= 1:
            spans[-1]["end"] = end_s
        else:
            spans.append({"src": src, "start": int(start_s), "end": int(end_s)})
    if not spans:
        return None

    total = max(1, int(total_end))
    dst = exports_dir / "_sfx_bed.wav"
    exports_dir.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [ffmpeg or "ffmpeg", "-y"]
    filters: list[str] = []
    labels: list[str] = []
    for i, span in enumerate(spans):
        cmd += ["-i", span["src"]]
        dur = max(1, span["end"] - span["start"])
        fade_out_at = max(0.0, dur - 0.8)
        filters.append(
            f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"aloop=loop=-1:size=2147483647,atrim=0:{dur},"
            f"afade=t=in:st=0:d=0.6,afade=t=out:st={fade_out_at:.2f}:d=0.8,"
            f"volume={volume_db}dB,adelay={span['start'] * 1000}:all=1[s{i}]"
        )
        labels.append(f"[s{i}]")
    # Silent base guarantees the bed spans the whole timeline.
    cmd += ["-f", "lavfi", "-t", str(total), "-i", "anullsrc=r=48000:cl=stereo"]
    labels.append(f"[{len(spans)}:a]")
    filters.append(
        "".join(labels) + f"amix=inputs={len(labels)}:normalize=0,atrim=0:{total}[out]"
    )
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[out]", "-c:a", "pcm_s16le", str(dst),
    ]
    _log(log, f"SFX: собираю подложку ({len(spans)} участков, {total}s)...")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        _log(log, f"SFX: сборка подложки не удалась: {(proc.stderr or '')[-300:]}")
        return None
    return dst
