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
                 seed: int | None = None, seconds: float | None = None) -> Path:
    """Generate one SFX clip from `prompt` via Woosh /generate (returns FLAC).

    Uses the distilled Woosh-DFlow model with the parameters from the reference
    client (api/test_api.py). Raises on failure; the caller treats any error as
    "no SFX for this shot".

    ``seconds`` is only sent when not None — the MOSS shim honours it (1..30s),
    the original Woosh server has a fixed clip length and must not receive it.
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
    if seconds is not None:
        payload["args"]["seconds"] = float(seconds)
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


SFX_TARGET_LUFS = -30.0

# Always-on quiet background so the bed never dips to full silence between
# ambience spans ("room tone" in film sound). Cached once per project.
ROOM_TONE_QUERY = "soft room tone, quiet air ambience, gentle background hum"
ROOM_TONE_FILE = "room_tone.flac"


def normalize_clip(path: Path, ffmpeg: str,
                   target_lufs: float = SFX_TARGET_LUFS,
                   log: Callable[[str], None] | None = None) -> bool:
    """Loudness-normalize one generated clip in place (non-fatal).

    Generated SFX vary a lot in loudness, so one fixed bed volume produces an
    uneven ambience between scenes. Normalizing every clip to a common target
    right after generation makes ``sfx_volume_db`` land at the same perceived
    level for every location. Any failure keeps the original file.
    """
    path = Path(path)
    tmp = path.with_suffix(".norm" + path.suffix)
    cmd = [
        ffmpeg or "ffmpeg", "-y", "-i", str(path),
        "-af", f"loudnorm=I={target_lufs}:TP=-2:LRA=11",
        "-ar", "48000", str(tmp),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(path)
            return True
        _log(log, f"SFX: нормализация клипа не удалась (код {proc.returncode}) — оставлен как есть.")
    except OSError as exc:
        _log(log, f"SFX: нормализация недоступна ({exc}) — клип оставлен как есть.")
    finally:
        tmp.unlink(missing_ok=True)
    return False


def ensure_room_tone(project_dir: Path, settings: Any,
                     log: Callable[[str], None] | None = None,
                     generate: bool = True) -> Path | None:
    """Return the project's cached room-tone clip, generating it once.

    Fully optional: disabled via ``sfx_room_tone`` setting, and any generation
    failure degrades to "no room tone" (the bed then behaves as before).
    """
    if not sfx_enabled(settings) or not bool(getattr(settings, "sfx_room_tone", True)):
        return None
    dst = project_dir / "sfx" / ROOM_TONE_FILE
    if dst.exists():
        return dst
    if not generate:
        return None
    woosh_url = (getattr(settings, "woosh_url", "") or "").strip()
    try:
        _log(log, "SFX: генерирую room tone (непрерывный тихий фон)...")
        generate_sfx(ROOM_TONE_QUERY, dst, woosh_url,
                     seed=int(_query_hash(ROOM_TONE_QUERY), 16) % (2**32))
        normalize_clip(dst, getattr(settings, "ffmpeg", "") or "ffmpeg", log=log)
        return dst
    except Exception as exc:
        _log(log, f"SFX: room tone недоступен ({exc}) — слой пропущен.")
        return None


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
    normalize_clip(dst, getattr(settings, "ffmpeg", "") or "ffmpeg")
    return dst


def generate_for_accent_query(project_dir: Path, query: str, settings: Any,
                              fresh: bool = False) -> Path:
    """Generate (or fetch the cached) one-shot accent clip for one query.

    Mirrors :func:`generate_for_query` but uses the accent cache namespace
    (``sfx_acc_*``) and asks duration-capable providers for a short clip —
    accents are events, not beds. Raises on failure (router turns it into 502).
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("Empty sfx accent query")
    woosh_url = (getattr(settings, "woosh_url", "") or "").strip()
    if not woosh_url:
        raise RuntimeError("Woosh URL is not configured")
    supports_seconds = provider_supports_seconds(woosh_url)
    key = _query_hash(query)
    sfx_dir = project_dir / "sfx"
    if fresh:
        import time
        dst = sfx_dir / f"sfx_acc_{key}_{int(time.time())}.flac"
        seed = None  # random take
    else:
        dst = sfx_dir / f"sfx_acc_{key}.flac"
        if dst.exists():
            return dst
        seed = int(key, 16) % (2**32)  # deterministic default take
    generate_sfx(
        query, dst, woosh_url, seed=seed,
        seconds=float(ACCENT_MAX_SECONDS) if supports_seconds else None,
    )
    normalize_clip(dst, getattr(settings, "ffmpeg", "") or "ffmpeg")
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
                normalize_clip(dst, getattr(settings, "ffmpeg", "") or "ffmpeg", log=log)
            except Exception as exc:
                _log(log, f"SFX: не удалось сгенерировать '{query}': {exc}")
                continue
        generated[key] = str(dst)
        card.sfx_output = str(dst)
        changed = True
    return changed


MAX_GEN_SECONDS = 30       # MOSS model card: stable output up to 30 s
GEN_BUCKET_SECONDS = 5     # cache granularity for generate-to-length clips

_SECONDS_SUPPORT: dict[str, bool] = {}


def _duration_bucket(seconds: float) -> int:
    """Round a span duration up to the cache bucket, capped at the model max."""
    import math

    bucket = int(math.ceil(max(1.0, float(seconds)) / GEN_BUCKET_SECONDS)) * GEN_BUCKET_SECONDS
    return min(MAX_GEN_SECONDS, bucket)


def provider_supports_seconds(woosh_url: str, timeout: int = 5) -> bool:
    """True when the SFX server can generate to a requested duration.

    The MOSS shim identifies itself in /ping with a ``provider`` field; the
    original Woosh server answers with a bare status and has a fixed clip
    length. The probe result is cached per URL for the process lifetime.
    """
    key = (woosh_url or "").rstrip("/")
    if key in _SECONDS_SUPPORT:
        return _SECONDS_SUPPORT[key]
    supports = False
    try:
        req = urllib.request.Request(key + "/ping", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        supports = bool(data.get("provider"))
    except Exception:
        supports = False
    _SECONDS_SUPPORT[key] = supports
    return supports


def ensure_sfx_for_spans(project_dir: Path,
                         cards_with_intervals: list[tuple[Any, tuple[int, int]]],
                         settings: Any,
                         log: Callable[[str], None] | None = None) -> bool:
    """Generate clips sized to their timeline spans (no loop seams).

    Groups consecutive cards that share one ``sfx_query`` into a span — the
    same grouping :func:`build_sfx_bed` later uses — buckets the span duration
    to :data:`GEN_BUCKET_SECONDS` steps capped at :data:`MAX_GEN_SECONDS`, and
    generates one clip per (query, bucket), cached as ``sfx_<hash>_<N>s.flac``.

    Providers without duration support (plain Woosh) keep the legacy fixed
    length and the legacy ``sfx_<hash>.flac`` cache name; the bed builder's
    loop+trim still guarantees full coverage either way. Cards that already
    have an existing sound (manual upload or take) are never replaced.
    Sets ``card.sfx_output`` in place; returns True if any card changed.
    """
    if not sfx_enabled(settings):
        return False
    woosh_url = (getattr(settings, "woosh_url", "") or "").strip()
    if not woosh_ping(woosh_url):
        _log(log, f"SFX: Woosh недоступен ({woosh_url}) — слой пропущен.")
        return False
    supports_seconds = provider_supports_seconds(woosh_url)

    # Consecutive cards with the same query form one span (same tolerance as
    # the bed builder's same-file merge).
    spans: list[dict] = []  # {query, start, end, cards}
    for card, (start_s, end_s) in cards_with_intervals:
        query = (getattr(card, "sfx_query", "") or "").strip()
        if not query:
            continue
        if (spans and spans[-1]["query"] == query
                and start_s - spans[-1]["end"] <= 1):
            spans[-1]["end"] = end_s
            spans[-1]["cards"].append(card)
        else:
            spans.append({"query": query, "start": int(start_s),
                          "end": int(end_s), "cards": [card]})

    sfx_dir = project_dir / "sfx"
    ffmpeg_path = getattr(settings, "ffmpeg", "") or "ffmpeg"
    changed = False
    for span in spans:
        need = [
            card for card in span["cards"]
            if not ((getattr(card, "sfx_output", "") or "").strip()
                    and Path(card.sfx_output).exists())
        ]
        if not need:
            continue
        key = _query_hash(span["query"])
        legacy = sfx_dir / f"sfx_{key}.flac"
        if supports_seconds:
            bucket = _duration_bucket(span["end"] - span["start"])
            dst = sfx_dir / f"sfx_{key}_{bucket}s.flac"
            gen_seconds: float | None = float(bucket)
        else:
            bucket = 0
            dst = legacy
            gen_seconds = None
        if not dst.exists():
            if not supports_seconds and legacy.exists():
                pass  # fixed-length provider: reuse the cached take
            else:
                try:
                    _log(log, f"SFX: генерирую '{span['query']}'"
                              + (f" ({bucket}s под спан)" if gen_seconds else "")
                              + "...")
                    # Deterministic seed per query: re-exports reuse the same
                    # sound, and every duration bucket is the same "take".
                    generate_sfx(span["query"], dst, woosh_url,
                                 seed=int(key, 16) % (2**32),
                                 seconds=gen_seconds)
                    normalize_clip(dst, ffmpeg_path, log=log)
                except Exception as exc:
                    _log(log, f"SFX: не удалось сгенерировать '{span['query']}': {exc}")
                    if legacy.exists():
                        dst = legacy  # older take is better than silence
                    else:
                        continue
        for card in need:
            card.sfx_output = str(dst)
            changed = True
    return changed


ACCENT_MAX_SECONDS = 5


def ensure_sfx_accents(project_dir: Path, cards: list, settings: Any,
                       log: Callable[[str], None] | None = None) -> bool:
    """Generate one-shot accent clips for cards with an ``sfx_accent_query``.

    Accents are events (a beep, a knock, a phone vibration), not beds: short
    clips cached per query as ``sfx_acc_<hash>.flac`` with a deterministic
    seed, played once at the shot start by :func:`build_sfx_bed`. Manually
    assigned accent files are never replaced.
    """
    if not sfx_enabled(settings):
        return False
    woosh_url = (getattr(settings, "woosh_url", "") or "").strip()
    if not woosh_ping(woosh_url):
        return False
    supports_seconds = provider_supports_seconds(woosh_url)
    sfx_dir = project_dir / "sfx"
    ffmpeg_path = getattr(settings, "ffmpeg", "") or "ffmpeg"
    changed = False
    generated: dict[str, str] = {}
    for card in cards:
        query = (getattr(card, "sfx_accent_query", "") or "").strip()
        if not query:
            continue
        existing = (getattr(card, "sfx_accent_output", "") or "").strip()
        if existing and Path(existing).exists():
            continue
        key = _query_hash(query)
        if key in generated:
            card.sfx_accent_output = generated[key]
            changed = True
            continue
        dst = sfx_dir / f"sfx_acc_{key}.flac"
        if not dst.exists():
            try:
                _log(log, f"SFX: генерирую акцент '{query}'...")
                generate_sfx(
                    query, dst, woosh_url,
                    seed=int(key, 16) % (2**32),
                    seconds=float(ACCENT_MAX_SECONDS) if supports_seconds else None,
                )
                normalize_clip(dst, ffmpeg_path, log=log)
            except Exception as exc:
                _log(log, f"SFX: не удалось сгенерировать акцент '{query}': {exc}")
                continue
        generated[key] = str(dst)
        card.sfx_accent_output = str(dst)
        changed = True
    return changed


def build_sfx_bed(cards_with_intervals: list[tuple[Any, tuple[int, int]]],
                  exports_dir: Path, ffmpeg: str, total_end: int,
                  volume_db: float = -24.0,
                  log: Callable[[str], None] | None = None,
                  room_tone: Path | None = None,
                  crossfade: float = 1.0) -> Path | None:
    """Assemble one project-length SFX bed wav from per-card clips.

    `cards_with_intervals` pairs each included card with its tiled [start, end)
    interval (seconds) — the same intervals the video segments use, so sound and
    picture line up. Consecutive shots that share the same sfx file are merged
    into one span so the ambience flows instead of restarting at every cut.

    Continuity rules (film-style):
    - adjacent spans hand over with a crossfade: the outgoing ambience is
      extended by ``crossfade`` seconds into the incoming one and fades out
      there while the next span fades in — no dip to silence on scene cuts;
    - an optional ``room_tone`` clip plays quietly under the whole timeline
      (volume_db - 8 dB) so gaps between spans are never fully silent.

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
    # Accent layer: one-shot event sounds at their shot start, never looped.
    accents: list[dict] = []
    for card, (start_s, end_s) in cards_with_intervals:
        src = (getattr(card, "sfx_accent_output", "") or "").strip()
        if not src or not Path(src).exists():
            continue
        accents.append({
            "src": src,
            "start": int(start_s),
            "dur": max(1.0, min(float(ACCENT_MAX_SECONDS), float(end_s - start_s))),
        })
    if not spans and not accents:
        return None

    total = max(1, int(total_end))
    # Crossfade hand-over: extend a span into its immediate neighbour so the
    # fades overlap. Spans separated by a gap keep their plain fade-out.
    xfade = max(0.0, float(crossfade))
    if xfade > 0:
        for i, span in enumerate(spans[:-1]):
            if spans[i + 1]["start"] - span["end"] <= 0.5:
                span["end"] = min(total, span["end"] + xfade)

    dst = exports_dir / "_sfx_bed.wav"
    exports_dir.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [ffmpeg or "ffmpeg", "-y"]
    filters: list[str] = []
    labels: list[str] = []
    for i, span in enumerate(spans):
        cmd += ["-i", span["src"]]
        dur = max(1.0, float(span["end"] - span["start"]))
        fade_out_at = max(0.0, dur - 0.8)
        filters.append(
            f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"aloop=loop=-1:size=2147483647,atrim=0:{dur:g},"
            f"afade=t=in:st=0:d=0.6,afade=t=out:st={fade_out_at:.2f}:d=0.8,"
            f"volume={volume_db}dB,adelay={span['start'] * 1000}:all=1[s{i}]"
        )
        labels.append(f"[s{i}]")
    input_count = len(spans)
    for accent in accents:
        cmd += ["-i", accent["src"]]
        filters.append(
            f"[{input_count}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"atrim=0:{accent['dur']:g},"
            f"afade=t=out:st={max(0.0, accent['dur'] - 0.4):.2f}:d=0.4,"
            # Accents must read over the bed: a one-shot event at bed level
            # would drown; +8 dB keeps it audible yet under the voice.
            f"volume={volume_db + 8.0}dB,adelay={accent['start'] * 1000}:all=1[a{input_count}]"
        )
        labels.append(f"[a{input_count}]")
        input_count += 1
    if room_tone is not None and Path(room_tone).exists():
        cmd += ["-i", str(room_tone)]
        filters.append(
            f"[{input_count}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"aloop=loop=-1:size=2147483647,atrim=0:{total},"
            f"afade=t=in:st=0:d=1,afade=t=out:st={max(0.0, total - 1.5):.2f}:d=1.5,"
            f"volume={volume_db - 8.0}dB[rt]"
        )
        labels.append("[rt]")
        input_count += 1
    # Silent base guarantees the bed spans the whole timeline.
    cmd += ["-f", "lavfi", "-t", str(total), "-i", "anullsrc=r=48000:cl=stereo"]
    labels.append(f"[{input_count}:a]")
    filters.append(
        "".join(labels) + f"amix=inputs={len(labels)}:normalize=0,atrim=0:{total}[out]"
    )
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[out]", "-c:a", "pcm_s16le", str(dst),
    ]
    _log(log, f"SFX: собираю подложку ({len(spans)} участков"
              + (f", акцентов: {len(accents)}" if accents else "")
              + f", {total}s"
              + (", room tone" if room_tone is not None and Path(room_tone).exists() else "")
              + ")...")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        _log(log, f"SFX: сборка подложки не удалась: {(proc.stderr or '')[-300:]}")
        return None
    return dst
