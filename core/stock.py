"""Stock-footage route.

Fetches candidate clips from Pexels / Pixabay, drops duplicates (an app-level
registry, perceptual-hash when Pillow is available, else exact source+id),
scores the survivors with a local vision model via LM Studio, trims the winner
to a short cut with ffmpeg, and drops it into the project's assets/videos.

Pure logic, no UI and no dependency on the desktop monolith. Pillow is optional
— without it, deduplication falls back to exact clip identity.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from core import lm
from core.config import ROOT
from core.project import atomic_write_text

try:  # Pillow is optional; perceptual dedup degrades gracefully without it.
    from PIL import Image  # type: ignore
    import io as _io
    _PIL = True
except Exception:  # pragma: no cover - import guard
    _PIL = False


LEGACY_REGISTRY_PATH = ROOT / "stock_registry.json"
_runtime_data_dir = os.environ.get("PIPELINE_DATA_DIR", "").strip()
_configured_registry = os.environ.get("PIPELINE_STOCK_REGISTRY", "").strip()
REGISTRY_PATH = (
    Path(_configured_registry).expanduser()
    if _configured_registry
    else (
        Path(_runtime_data_dir).expanduser() / "stock_registry.json"
        if _runtime_data_dir
        else LEGACY_REGISTRY_PATH
    )
)
_UA = {"User-Agent": "PipelineStudio/1.0"}


# ─── logging helper ──────────────────────────────────────────────────
def _log(log: Callable[[str], None] | None, msg: str) -> None:
    if log:
        log(msg)


# ─── HTTP ────────────────────────────────────────────────────────────
def _get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={**_UA, **(headers or {})}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _download(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=_UA, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ─── source clients ──────────────────────────────────────────────────
def _pick_pexels_file(video_files: list[dict]) -> dict | None:
    """Prefer an mp4 with the largest width up to 1920 (falls back to any)."""
    mp4s = [f for f in video_files if (f.get("file_type") or "").endswith("mp4") and f.get("link")]
    pool = mp4s or [f for f in video_files if f.get("link")]
    if not pool:
        return None
    capped = [f for f in pool if (f.get("width") or 0) <= 1920]
    chosen = max(capped or pool, key=lambda f: f.get("width") or 0)
    return chosen


def search_pexels(query: str, api_key: str, per_page: int = 15, timeout: int = 20) -> list[dict]:
    if not api_key:
        return []
    q = urllib.parse.urlencode({"query": query, "per_page": per_page, "orientation": "landscape"})
    url = f"https://api.pexels.com/videos/search?{q}"
    data = _get_json(url, headers={"Authorization": api_key}, timeout=timeout)
    out: list[dict] = []
    for v in data.get("videos", []):
        vf = _pick_pexels_file(v.get("video_files", []) or [])
        if not vf:
            continue
        out.append({
            "source": "pexels",
            "id": str(v.get("id", "")),
            "preview_url": v.get("image", ""),
            "video_url": vf.get("link", ""),
            "width": int(vf.get("width") or 0),
            "height": int(vf.get("height") or 0),
            "duration": float(v.get("duration") or 0),
            "page_url": v.get("url", ""),
            "author": str((v.get("user") or {}).get("name", "") or ""),
            "license": "Pexels license",
        })
    return out


def search_pixabay(query: str, api_key: str, per_page: int = 15, timeout: int = 20) -> list[dict]:
    if not api_key:
        return []
    q = urllib.parse.urlencode({
        "key": api_key, "q": query, "per_page": max(3, min(per_page, 200)),
        "video_type": "film",
    })
    url = f"https://pixabay.com/api/videos/?{q}"
    data = _get_json(url, timeout=timeout)
    out: list[dict] = []
    for h in data.get("hits", []):
        vids = h.get("videos", {}) or {}
        size = vids.get("medium") or vids.get("large") or vids.get("small") or vids.get("tiny") or {}
        link = size.get("url", "")
        if not link:
            continue
        preview = size.get("thumbnail", "") or (vids.get("large") or {}).get("thumbnail", "")
        out.append({
            "source": "pixabay",
            "id": str(h.get("id", "")),
            "preview_url": preview,
            "video_url": link,
            "width": int(size.get("width") or 0),
            "height": int(size.get("height") or 0),
            "duration": float(h.get("duration") or 0),
            "page_url": h.get("pageURL", ""),
            "author": str(h.get("user", "") or ""),
            "license": "Pixabay Content License",
        })
    return out


def search_wikimedia(query: str, per_page: int = 8, timeout: int = 20) -> list[dict]:
    """Wikimedia Commons video search (free, no key). Mostly CC-licensed webm;
    the trim/re-encode step normalizes the container anyway."""
    q = urllib.parse.urlencode({
        "action": "query", "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:video {query}",
        "gsrnamespace": 6,
        "gsrlimit": max(1, min(per_page, 20)),
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": 640,
    })
    data = _get_json(f"https://commons.wikimedia.org/w/api.php?{q}", timeout=timeout)
    out: list[dict] = []
    for page in (data.get("query", {}).get("pages", {}) or {}).values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        url = info.get("url", "")
        if not url:
            continue
        meta = info.get("extmetadata", {}) or {}
        out.append({
            "source": "wikimedia",
            "id": str(page.get("pageid", "")),
            "preview_url": info.get("thumburl", ""),
            "video_url": url,
            "width": int(info.get("width") or 0),
            "height": int(info.get("height") or 0),
            "duration": 0.0,
            "page_url": info.get("descriptionurl", ""),
            "license": str((meta.get("LicenseShortName") or {}).get("value", "")),
            "author": str((meta.get("Artist") or {}).get("value", "")),
        })
    return out


def search_nasa(query: str, per_page: int = 5, timeout: int = 20) -> list[dict]:
    """NASA Image and Video Library (free, no key, public domain). Resolving the
    actual mp4 costs one extra request per item, so per_page stays small."""
    q = urllib.parse.urlencode({"q": query, "media_type": "video", "page_size": max(1, min(per_page, 10))})
    data = _get_json(f"https://images-api.nasa.gov/search?{q}", timeout=timeout)
    out: list[dict] = []
    for item in (data.get("collection", {}).get("items", []) or [])[:per_page]:
        meta = (item.get("data") or [{}])[0]
        nasa_id = str(meta.get("nasa_id", ""))
        links = item.get("links") or []
        preview = next(
            (
                link.get("href", "")
                for link in links
                if "image" in str(link.get("render", ""))
            ),
            "",
        )
        try:
            files = _get_json(item.get("href", ""), timeout=timeout)
        except Exception:
            continue
        mp4s = [f for f in files if isinstance(f, str) and f.lower().endswith(".mp4")]
        if not mp4s:
            continue
        # Prefer the mobile rendition (small, plenty for a 1080p crop of b-roll);
        # fall back to any mp4.
        video_url = next((f for f in mp4s if "~mobile" in f.lower()), mp4s[0])
        out.append({
            "source": "nasa",
            "id": nasa_id,
            "preview_url": preview,
            "video_url": video_url,
            "width": 0, "height": 0, "duration": 0.0,
            "page_url": f"https://images.nasa.gov/details/{nasa_id}",
            "license": "public domain",
            "author": str(meta.get("photographer", "") or meta.get("center", "") or "NASA"),
        })
    return out


def search_archive(query: str, per_page: int = 5, timeout: int = 20) -> list[dict]:
    """Internet Archive movies search (free, no key) — includes the Prelinger
    archives, i.e. real historical footage. One metadata request per item."""
    q = urllib.parse.urlencode({
        "q": f"({query}) AND mediatype:(movies)",
        "fl[]": "identifier",
        "rows": max(1, min(per_page, 10)),
        "page": 1,
        "output": "json",
    })
    data = _get_json(f"https://archive.org/advancedsearch.php?{q}", timeout=timeout)
    docs = (data.get("response", {}) or {}).get("docs", []) or []
    out: list[dict] = []
    for doc in docs[:per_page]:
        ident = str(doc.get("identifier", ""))
        if not ident:
            continue
        try:
            meta = _get_json(f"https://archive.org/metadata/{ident}", timeout=timeout)
        except Exception:
            continue
        mp4s = [
            f for f in (meta.get("files") or [])
            if str(f.get("name", "")).lower().endswith(".mp4")
        ]
        if not mp4s:
            continue
        # Smallest mp4 above 1MB: usually the web-friendly derivative, not the raw scan.
        def _size(f: dict) -> int:
            try:
                return int(f.get("size") or 0)
            except (TypeError, ValueError):
                return 0
        usable = sorted((f for f in mp4s if _size(f) > 1_000_000), key=_size) or mp4s
        name = urllib.parse.quote(str(usable[0].get("name", "")))
        out.append({
            "source": "archive",
            "id": ident,
            "preview_url": f"https://archive.org/services/img/{ident}",
            "video_url": f"https://archive.org/download/{ident}/{name}",
            "width": 0, "height": 0, "duration": 0.0,
            "page_url": f"https://archive.org/details/{ident}",
            "license": str(meta.get("metadata", {}).get("licenseurl", "") or ""),
            "author": str(meta.get("metadata", {}).get("creator", "") or ""),
        })
    return out


def gather_candidates(query: str, settings: Any, per_source: int = 12,
                      log: Callable[[str], None] | None = None) -> list[dict]:
    """Merge Pexels + Pixabay (+ optional free/archival sources) results for one
    query. Each source failure is logged and skipped so one bad key never blocks
    the others."""
    candidates: list[dict] = []
    pexels_key = getattr(settings, "pexels_api_key", "") or ""
    pixabay_key = getattr(settings, "pixabay_api_key", "") or ""
    if pexels_key:
        try:
            candidates += search_pexels(query, pexels_key, per_page=per_source)
        except Exception as exc:
            _log(log, f"Pexels недоступен: {exc}")
    if pixabay_key:
        try:
            candidates += search_pixabay(query, pixabay_key, per_page=per_source)
        except Exception as exc:
            _log(log, f"Pixabay недоступен: {exc}")
    if bool(getattr(settings, "stock_free_sources", False)):
        free_n = max(3, per_source // 2)
        for name, fn in (("Wikimedia", search_wikimedia), ("NASA", search_nasa), ("Archive.org", search_archive)):
            try:
                candidates += fn(query, free_n)
            except Exception as exc:
                _log(log, f"{name} недоступен: {exc}")
    return candidates


# ─── deduplication registry ──────────────────────────────────────────
def load_registry() -> dict:
    candidates = [REGISTRY_PATH]
    if REGISTRY_PATH.resolve() != LEGACY_REGISTRY_PATH.resolve():
        candidates.append(LEGACY_REGISTRY_PATH)
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("clips", []), list):
                return data
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return {"clips": []}


def save_registry(reg: dict) -> None:
    try:
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            REGISTRY_PATH,
            json.dumps(reg, ensure_ascii=False, indent=2),
        )
    except OSError:
        pass


def average_hash(image_bytes: bytes) -> int | None:
    """8x8 average hash as a 64-bit int. Returns None if Pillow is unavailable
    or the image can't be decoded."""
    if not _PIL:
        return None
    try:
        img = Image.open(_io.BytesIO(image_bytes)).convert("L").resize((8, 8), Image.BILINEAR)
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        bits = 0
        for i, p in enumerate(pixels):
            if p >= avg:
                bits |= (1 << i)
        return bits
    except Exception:
        return None


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def is_duplicate(candidate: dict, phash: int | None, reg: dict, max_bits: int = 6) -> bool:
    for clip in reg.get("clips", []):
        if clip.get("source") == candidate.get("source") and clip.get("id") == candidate.get("id"):
            return True
        if phash is not None and clip.get("phash") is not None:
            try:
                if hamming(int(clip["phash"]), phash) <= max_bits:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def register_clip(reg: dict, candidate: dict, phash: int | None) -> None:
    reg.setdefault("clips", []).append({
        "source": candidate.get("source"),
        "id": candidate.get("id"),
        "phash": phash,
        "query": candidate.get("_query", ""),
        "ts": int(time.time()),
    })


# ─── vision scoring (LM Studio, OpenAI-compatible) ───────────────────
_SCORE_PROMPT = (
    "You are a video editor picking B-roll. Rate how well this clip's preview "
    "frame matches the shot description. Penalise heavily any visible watermark, "
    "on-screen text, logo, or a generic/cliché stock look. "
    "Reply ONLY with JSON: {\"score\": <0..1>, \"watermark\": <true|false>}.\n"
    "Shot description: "
)


class _StockVisionScore(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    score: float = Field(ge=0.0, le=1.0)
    watermark: bool


def score_candidate(preview_bytes: bytes, description: str, lm_url: str,
                    vision_model: str, timeout: int = 40, ttl: int | None = None) -> float:
    """Ask a local vision model to rate 0..1. Returns 0.0 on any failure so a
    broken probe can never win selection. `ttl` sets LM Studio's idle auto-unload
    (JIT) so the vision model leaves VRAM shortly after the batch, same mechanism
    the scene/shot LLM calls use."""
    try:
        settings = SimpleNamespace(
            lm_url=lm_url,
            lm_model=vision_model,
            lm_auto_unload=False,
        )
        reply = lm.chat_structured(
            settings,
            [{"role": "user", "content": _SCORE_PROMPT + (description or "")}],
            _StockVisionScore,
            model=vision_model,
            image_bytes=preview_bytes,
            transport="openai",
            temperature=0.1,
            max_tokens=160,
            timeout=timeout,
            ttl=ttl,
            schema_name="stock_vision_score",
        )
        score = reply.value.score
        if reply.value.watermark:
            score *= 0.3
        return score
    except Exception:
        return 0.0


# ─── trimming ────────────────────────────────────────────────────────
def _tc_to_seconds(tc: str) -> float:
    try:
        parts = [int(p) for p in str(tc).split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, s = parts[-3], parts[-2], parts[-1]
        return h * 3600 + m * 60 + s
    except Exception:
        return 0.0


def _probe_duration(path: Path, ffmpeg: str) -> float:
    """Clip length in seconds via ffprobe (assumed next to ffmpeg). 0 on failure."""
    probe = "ffprobe"
    if ffmpeg and "ffmpeg" in ffmpeg.lower():
        probe = ffmpeg[:ffmpeg.lower().rfind("ffmpeg")] + "ffprobe" + ffmpeg[ffmpeg.lower().rfind("ffmpeg") + 6:]
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        return float((out.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _uniquify_filter(mirror: bool = False) -> str:
    """A subtle, tasteful ffmpeg filter chain that makes a stock clip look graded
    (not raw stock) and slightly unique per instance: a small random crop+zoom
    (also trims edge watermarks), a mild colour grade, and a micro hue shift.
    `mirror` adds a horizontal flip (stronger uniquifier, off by default)."""
    crop_f = round(random.uniform(0.92, 0.96), 3)   # crop 4-8%, re-scaled by export
    hue = round(random.uniform(-4.0, 4.0), 1)
    parts = [
        f"crop=iw*{crop_f}:ih*{crop_f}",
        "eq=contrast=1.06:saturation=1.08:brightness=0.01:gamma=0.98",
        f"hue=h={hue}",
    ]
    if mirror:
        parts.append("hflip")
    return ",".join(parts)


def build_stock_clip(src: Path, dst: Path, ffmpeg: str, slot_seconds: float,
                    ceiling: float = 0.0, offset: float = 0.7,
                    uniquify: bool = True, mirror: bool = False) -> Path:
    """Produce a clip whose length equals the timeline slot, so the export never
    freezes the last frame. If the source is long enough, cut a slot-length
    segment (skipping a short intro); if it is shorter than the slot, loop it to
    fill. ``ceiling`` (>0) caps the maximum length as a safety limit; 0 = fill
    the whole slot. ``uniquify`` bakes in a subtle grade + crop so the clip is
    less recognizable as raw stock. Audio is dropped — export handles voice/music.
    """
    ff = ffmpeg or "ffmpeg"
    target = float(slot_seconds) if slot_seconds and slot_seconds > 0 else (ceiling or 5.0)
    if ceiling and ceiling > 0:
        target = min(target, float(ceiling))
    target = max(0.5, target)

    src_dur = _probe_duration(src, ff)
    usable = max(0.0, src_dur - offset) if src_dur else 0.0
    dst.parent.mkdir(parents=True, exist_ok=True)
    enc = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    vf_args = ["-vf", _uniquify_filter(mirror)] if uniquify else []

    if src_dur and usable < target:
        # Source shorter than the slot -> loop it to fill (start at 0, no offset).
        loops = int(target // max(0.1, src_dur)) + 2
        cmd = [ff, "-y", "-stream_loop", str(loops), "-i", str(src),
               "-t", f"{target:.2f}", *vf_args, *enc, str(dst)]
    else:
        cmd = [ff, "-y", "-ss", f"{max(0.0, offset):.2f}", "-i", str(src),
               "-t", f"{target:.2f}", *vf_args, *enc, str(dst)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg trim failed: {proc.stderr[-400:] if proc.stderr else 'unknown error'}")
    return dst


# ─── orchestration ───────────────────────────────────────────────────
def stock_enabled(settings: Any) -> bool:
    return bool(
        getattr(settings, "pexels_api_key", "")
        or getattr(settings, "pixabay_api_key", "")
        or getattr(settings, "stock_free_sources", False)
    )


def _mostly_ascii(text: str) -> bool:
    """True if the string is predominantly latin/ASCII — a cheap proxy for
    'searchable on Pexels/Pixabay'. Cyrillic descriptions return noise, so they
    must not be used as a stock query."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    ascii_letters = sum(1 for c in letters if c.isascii())
    return ascii_letters / len(letters) >= 0.8


def _vision_ttl(settings: Any) -> int | None:
    """TTL (seconds) for the vision model's LM Studio JIT auto-unload — same
    mechanism the scene/shot LLM calls use. Respects lm_auto_unload; when the
    user's unload delay is 0 (immediate) we still use a small positive TTL so a
    per-card batch doesn't reload the model each time — it then leaves VRAM
    shortly after the batch finishes."""
    auto = getattr(settings, "lm_auto_unload", True)
    if isinstance(auto, str):
        auto = auto.strip().lower() not in {"0", "false", "no", "off"}
    if not auto:
        return None
    try:
        delay = int(str(getattr(settings, "lm_unload_delay", "0")).strip() or "0")
    except ValueError:
        delay = 60
    # delay 0 (immediate) -> 20s floor: long enough to survive the per-card gaps
    # of a batch (a few seconds each), short enough to free VRAM soon after.
    return delay if delay > 0 else 20


def _queries_for_card(card: Any) -> list[str]:
    """Build stock search queries. Prefer the English `asset_query`; only fall
    back to `description` if it is English (a Russian editor-facing description
    pollutes the search and returns unrelated clips)."""
    queries: list[str] = []
    aq = (getattr(card, "asset_query", "") or "").strip()
    if aq and _mostly_ascii(aq):
        queries.append(aq)
    if not queries:
        desc = (getattr(card, "description", "") or "").strip()
        if desc and _mostly_ascii(desc):
            queries.append(desc)
    # Last resort: a generic English b-roll query rather than a Cyrillic one.
    return queries[:2] or ["documentary b-roll"]


def _prefetch_previews(candidates: list[dict], workers: int = 8,
                       timeout: int = 15) -> dict[int, bytes]:
    """Download candidate previews in parallel (pure I/O). Sequential preview
    fetching used to dominate stock time: 20 candidates x network latency per
    card. Returns {candidate_index: bytes}; failures are simply absent."""
    from concurrent.futures import ThreadPoolExecutor

    def fetch(i: int) -> tuple[int, bytes]:
        url = candidates[i].get("preview_url", "")
        if not url:
            return i, b""
        try:
            return i, _download(url, timeout=timeout)
        except Exception:
            return i, b""

    out: dict[int, bytes] = {}
    if not candidates:
        return out
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(candidates)))) as pool:
        for i, data in pool.map(fetch, range(len(candidates))):
            if data:
                out[i] = data
    return out


def _score_pool(candidates: list[dict], reg: dict, blind: bool, vision_model: str,
                description: str, lm_url: str, ttl: int | None,
                accept: float = 0.95):
    """Score one pool of candidates against the registry. Returns
    (best_card, best_score, best_phash, seen_after_dedup). Blind mode: the first
    non-duplicate wins at 0.5. Scoring STOPS as soon as a candidate clears
    `accept` — that threshold literally means "good enough, take it and stop",
    so burning a vision call on every remaining preview is pure waste."""
    best: dict | None = None
    best_score = -1.0
    best_phash: int | None = None
    seen = 0
    previews = {} if blind else _prefetch_previews(candidates)
    for i, c in enumerate(candidates):
        preview = previews.get(i, b"")
        phash: int | None = None
        if blind and c.get("preview_url"):
            try:
                preview = _download(c["preview_url"], timeout=15)
            except Exception:
                preview = b""
        if preview:
            phash = average_hash(preview)
        if is_duplicate(c, phash, reg):
            continue
        seen += 1
        if blind:
            return c, 0.5, phash, seen
        if not preview:
            continue
        score = score_candidate(preview, description, lm_url, vision_model, ttl=ttl)
        if score > best_score:
            best, best_score, best_phash = c, score, phash
        if score >= accept:  # good enough — stop scoring the rest of this pool
            break
    return best, best_score, best_phash, seen


def _rephrase_query(description: str, tried: list[str], lm_url: str, model: str,
                    ttl: int | None = None, timeout: int = 60) -> str:
    """Ask the chat LLM for a DIFFERENT English stock-search phrase when the
    current query found nothing suitable. Returns '' on failure."""
    prompt = (
        "A stock-video search for a scene returned nothing suitable. "
        "Give ONE different, simpler and broader English search phrase (2-4 words) "
        "for the same scene — a concrete filmable subject + place. Do NOT repeat any "
        "already-tried phrase; no style or genre words.\n"
        f"Scene: {description}\n"
        f"Already tried: {', '.join(tried) or '(none)'}\n"
        'Reply with JSON only: {"query": "..."}'
    )
    payload = {
        "model": (model or "local-model").strip(),
        "messages": [
            {"role": "system", "content": "Return only valid JSON. No markdown."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.5,
    }
    if ttl is not None and ttl > 0:
        payload["ttl"] = ttl
    try:
        req = urllib.request.Request(
            lm_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        content = data["choices"][0]["message"]["content"]
        s, e = content.find("{"), content.rfind("}")
        obj = json.loads(content[s:e + 1]) if s >= 0 and e > s else {}
        q = str(obj.get("query", "")).strip()
        return q if q and _mostly_ascii(q) else ""
    except Exception:
        return ""


def fetch_stock_for_card(project_dir: Path, card: Any, settings: Any,
                        log: Callable[[str], None] | None = None) -> dict | None:
    """Resolve one card to a stock clip via a layered search: score the first
    query's pool; accept the best if it clears `accept`; keep the best across
    rounds; if nothing clears `accept`, rephrase the query and try more rounds;
    finally accept the overall best if it clears `floor`, else return None (the
    caller then falls back to Ken Burns / AI)."""
    if not stock_enabled(settings):
        _log(log, "Сток отключён: не задан ни один API-ключ (Pexels/Pixabay).")
        return None

    accept = float(getattr(settings, "stock_score_threshold", 0.6) or 0.6)
    floor = float(getattr(settings, "stock_floor", 0.4) or 0.4)
    if floor > accept:
        floor = accept
    retries = max(0, int(getattr(settings, "stock_retry_queries", 1) or 0))
    per_source = int(getattr(settings, "stock_candidates", 20) or 20) // 2 or 6
    vision_model = (getattr(settings, "lm_vision_model", "") or "").strip()
    chat_model = (getattr(settings, "lm_model", "") or "local-model").strip()
    lm_url = getattr(settings, "lm_url", "http://127.0.0.1:1234/v1")
    vision_ttl = _vision_ttl(settings)
    description = getattr(card, "description", "") or getattr(card, "screen_text", "")
    blind = not vision_model
    if blind:
        _log(log, "Сток: vision-модель не задана — слепой режим (первый неповторный клип).")

    reg = load_registry()
    best: dict | None = None
    best_score = -1.0
    best_phash: int | None = None
    total_seen = 0
    tried: list[str] = []
    round_i = 0
    while True:
        if round_i == 0:
            qs = _queries_for_card(card)
            tried.extend(qs)
            pool: list[dict] = []
            for q in qs:
                for c in gather_candidates(q, settings, per_source=per_source, log=log):
                    c["_query"] = q
                    pool.append(c)
        else:
            if blind:
                break  # blind mode can't judge relevance, so retrying is pointless
            newq = _rephrase_query(description, tried, lm_url, chat_model, ttl=vision_ttl)
            if not newq or newq.lower() in {t.lower() for t in tried}:
                _log(log, "Сток: перефраз не дал нового запроса — останавливаюсь.")
                break
            tried.append(newq)
            _log(log, f"Сток: ничего не прошло — меняю запрос → '{newq}' (раунд {round_i + 1})...")
            pool = []
            for c in gather_candidates(newq, settings, per_source=per_source * 2, log=log):
                c["_query"] = newq
                pool.append(c)

        rb, rs, rp, seen = _score_pool(pool, reg, blind, vision_model, description, lm_url, vision_ttl, accept=accept)
        total_seen += seen
        if rb is not None and rs > best_score:
            best, best_score, best_phash = rb, rs, rp
        if best is not None and best_score >= accept:
            break  # good enough — stop, don't waste retries
        if round_i >= retries:
            break
        round_i += 1

    if total_seen == 0:
        _log(log, "Сток: кандидатов не найдено (или все уже использованы).")
        return None
    gate = 0.0 if blind else floor
    if best is None or best_score < gate:
        _log(log, f"Сток: лучший {max(best_score, 0):.2f} ниже пола {gate:.2f} после {len(tried)} запрос(ов).")
        return None

    shot_index = int(getattr(card, "shot_index", 0) or 0)
    videos_dir = project_dir / "assets" / "videos"
    tmp_path = videos_dir / f"shot_{shot_index:03d}_stock_src.mp4"
    dst_path = videos_dir / f"shot_{shot_index:03d}_stock.mp4"
    videos_dir.mkdir(parents=True, exist_ok=True)

    _log(log, f"Сток: качаю лучший клип ({best['source']}, score {best_score:.2f})...")
    tmp_path.write_bytes(_download(best["video_url"], timeout=180))

    ffmpeg = getattr(settings, "ffmpeg", "") or "ffmpeg"
    ceiling = float(getattr(settings, "stock_max_seconds", 0) or 0)
    uniquify = bool(getattr(settings, "stock_uniquify", True))
    mirror = bool(getattr(settings, "stock_mirror", False))
    slot = _tc_to_seconds(getattr(card, "end", "")) - _tc_to_seconds(getattr(card, "start", ""))
    _log(log, f"Сток: подгоняю клип под слот {slot:.0f}с" + (f" (потолок {ceiling:.0f}с)" if ceiling else "") + (" +уникализация" if uniquify else ""))
    build_stock_clip(tmp_path, dst_path, ffmpeg, slot, ceiling=ceiling, uniquify=uniquify, mirror=mirror)
    try:
        tmp_path.unlink(missing_ok=True)
    except Exception:
        pass

    register_clip(reg, best, best_phash)
    save_registry(reg)

    return {
        "video_output": str(dst_path),
        "source_url": best.get("page_url", "") or best.get("video_url", ""),
        "stock_score": round(best_score, 3),
        "source": best.get("source", ""),
        "license": best.get("license", ""),
        "author": best.get("author", ""),
    }
