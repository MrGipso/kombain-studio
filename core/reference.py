"""Reference-insert route.

Turns a spoken reference to a concrete, nameable entity (a technology, a paper,
a person, an organisation, a concept) into a short clip that shows the *real*
source page — a Wikipedia article or an arXiv paper — sliding in from the side
over a blurred backdrop. The LLM only names the entity (`reference_query`) and
the source (`reference_source`); this module resolves it to a real URL, captures
it, and composites the clip.

Why it exists: it fixes three things at once — real screens carry *real readable
text* (Krea cannot render text), it adds a credible on-screen citation, and it
breaks up a wall of AI renders with genuine UI. The whole route degrades
gracefully: no Playwright -> a clean rendered card from the page's API summary;
no match at all -> the caller falls back to Ken Burns / AI.

Dependency-light: page resolution uses only urllib (like core/stock.py).
Playwright and Pillow are both optional and guarded.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

try:  # Pillow is optional: enables rounded panels, shadows and the API-card fallback.
    from PIL import Image, ImageDraw, ImageFont, ImageFilter  # type: ignore
    _PIL = True
except Exception:  # pragma: no cover - import guard
    _PIL = False

_UA = {"User-Agent": "PipelineStudio/1.0 (reference-insert)"}

# CSS injected before capture to strip chrome that reads as "a wiki page being
# edited" rather than "a clean reference": edit links, notices, nav rails.
_CLEANUP_CSS = (
    ".mw-editsection,#siteNotice,.mw-indicators,#mw-navigation,.vector-header,"
    ".vector-page-toolbar,.vector-column-start,#mw-panel,.mw-jump-link,"
    ".cookie-banner,#centralNotice,.noprint{display:none!important}"
    "body{background:#fff!important}"
)
# Preferred content elements to clip the screenshot to (first that exists wins).
_CONTENT_SELECTORS = ("#content", "main.mw-body", "main", "#bodyContent", "article")


# ─── logging ─────────────────────────────────────────────────────────
def _log(log: Callable[[str], None] | None, msg: str) -> None:
    if log:
        log(msg)


# ─── HTTP ────────────────────────────────────────────────────────────
def _get_json(url: str, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, headers=_UA, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _get_text(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=_UA, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _download(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=_UA, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ─── timecode ────────────────────────────────────────────────────────
def _tc_to_seconds(tc: str) -> float:
    try:
        parts = [int(p) for p in str(tc).split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, s = parts[-3], parts[-2], parts[-1]
        return h * 3600 + m * 60 + s
    except Exception:
        return 0.0


# ─── query / language helpers ────────────────────────────────────────
def wiki_lang(detected: str) -> str:
    """Map a project language code to a Wikipedia subdomain. Unknown -> 'en'."""
    code = (detected or "").strip().lower()[:2]
    # A small allowlist keeps us from pointing at a non-existent subdomain.
    known = {"ru", "en", "de", "fr", "es", "it", "pt", "pl", "uk", "nl", "ja", "zh"}
    return code if code in known else "en"


def _clean_query(query: str) -> str:
    q = re.sub(r"\s+", " ", (query or "").strip())
    # Strip wrapping quotes the LLM sometimes adds.
    return q.strip("\"'«» ")


def _title_relevant(query: str, title: str) -> bool:
    """Guard against fuzzy opensearch mismatches. Wikipedia's opensearch always
    returns the *closest* title even when it is unrelated — e.g. 'LM Studio'
    (no article) resolves to 'StudioCanal', 'llama.cpp' to 'Llama'. Require that
    most of the query's word tokens appear as tokens in the resolved title;
    otherwise the match is noise and the reference is skipped (caller falls back
    to Ken Burns / AI)."""
    def toks(s: str) -> list[str]:
        return [t for t in re.split(r"[^0-9a-zа-яё]+", s.lower()) if len(t) >= 2]
    q = toks(query)
    t = set(toks(title))
    if not q or not t:
        return False
    hits = sum(1 for w in q if w in t)
    return hits / len(q) >= 0.6


# ─── resolvers ───────────────────────────────────────────────────────
def resolve_wikipedia(query: str, lang: str = "en", timeout: int = 15,
                      log: Callable[[str], None] | None = None) -> dict | None:
    """Resolve a free-text query to a real Wikipedia article: opensearch picks
    the best title, then the REST summary gives the canonical URL, extract and
    lead image. Falls back to English if the localised wiki has no match."""
    q = _clean_query(query)
    if not q:
        return None

    def _try(lc: str) -> dict | None:
        base = f"https://{lc}.wikipedia.org"
        search = base + "/w/api.php?" + urllib.parse.urlencode({
            "action": "opensearch", "search": q, "limit": 1,
            "namespace": 0, "format": "json",
        })
        try:
            data = _get_json(search, timeout=timeout)
        except Exception as exc:
            _log(log, f"Википедия ({lc}) недоступна: {exc}")
            return None
        titles = data[1] if isinstance(data, list) and len(data) > 1 else []
        if not titles:
            return None
        title = titles[0]
        if not _title_relevant(q, title):
            _log(log, f"Википедия ({lc}): '{title}' не относится к запросу '{q}' — пропускаю.")
            return None
        summ_url = base + "/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"))
        try:
            summ = _get_json(summ_url, timeout=timeout)
        except Exception:
            summ = {}
        page_url = (
            ((summ.get("content_urls") or {}).get("desktop") or {}).get("page")
            or f"{base}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
        )
        return {
            "source": "wikipedia",
            "title": summ.get("title") or title,
            "url": page_url,
            "extract": summ.get("extract", "") or "",
            "thumbnail": ((summ.get("thumbnail") or {}).get("source") or ""),
            "lang": lc,
            "credit": f"{summ.get('title') or title} — Wikipedia (CC BY-SA)",
        }

    hit = _try(lang)
    if not hit and lang != "en":
        _log(log, f"Википедия: в '{lang}' статьи нет — пробую английскую.")
        hit = _try("en")
    if hit:
        _log(log, f"Википедия: '{hit['title']}' → {hit['url']}")
    return hit


def resolve_arxiv(query: str, timeout: int = 15,
                  log: Callable[[str], None] | None = None) -> dict | None:
    """Resolve a query to the closest arXiv paper via the Atom search API."""
    q = _clean_query(query)
    if not q:
        return None
    url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode({
        "search_query": f'all:"{q}"', "start": 0, "max_results": 1,
    })
    try:
        xml = _get_text(url, timeout=timeout)
        root = ET.fromstring(xml)
    except Exception as exc:
        _log(log, f"arXiv недоступен: {exc}")
        return None
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = root.find("a:entry", ns)
    if entry is None:
        return None
    title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
    abs_url = (entry.findtext("a:id", default="", namespaces=ns) or "").strip()
    summary = " ".join((entry.findtext("a:summary", default="", namespaces=ns) or "").split())
    if not abs_url:
        return None
    _log(log, f"arXiv: '{title}' → {abs_url}")
    return {
        "source": "arxiv",
        "title": title,
        "url": abs_url,
        "extract": summary,
        "thumbnail": "",
        "lang": "en",
        "credit": f"{title} — arXiv.org",
    }


def resolve_reference(card: Any, settings: Any, lang: str = "en",
                      log: Callable[[str], None] | None = None) -> dict | None:
    """Dispatch a card's reference_query to the right source (wikipedia|arxiv).
    Wikipedia is the default and the fallback if an unknown source is given."""
    query = getattr(card, "reference_query", "") or getattr(card, "asset_query", "") or ""
    source = (getattr(card, "reference_source", "") or "wikipedia").strip().lower()
    if source == "arxiv":
        hit = resolve_arxiv(query, log=log)
        if hit:
            return hit
        _log(log, "arXiv: не найдено — пробую Википедию.")
    return resolve_wikipedia(query, lang=lang, log=log)


# ─── capture ─────────────────────────────────────────────────────────
def _playwright_shot(url: str, dst: Path, timeout_ms: int = 30000,
                     log: Callable[[str], None] | None = None) -> Path | None:
    """Screenshot the top of a page with headless Chromium, cropped to the main
    content element and de-cluttered. Returns None if Playwright is unavailable
    or the capture fails (the caller then renders an API card instead)."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        _log(log, "Playwright не установлен — рисую карточку из API-данных.")
        return None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1280, "height": 1400},
                                    device_scale_factor=2)
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            try:
                page.add_style_tag(content=_CLEANUP_CSS)
            except Exception:
                pass
            page.wait_for_timeout(400)
            target = None
            for sel in _CONTENT_SELECTORS:
                el = page.query_selector(sel)
                if el:
                    target = el
                    break
            dst.parent.mkdir(parents=True, exist_ok=True)
            if target is not None:
                target.screenshot(path=str(dst))
            else:  # no known content box -> top of the page
                page.screenshot(path=str(dst),
                                clip={"x": 0, "y": 0, "width": 1280, "height": 1200})
            browser.close()
        return dst if dst.exists() else None
    except Exception as exc:
        _log(log, f"Playwright: снимок не удался ({exc}) — карточка из API-данных.")
        return None


def _wrap_text(draw, text: str, font, max_w: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _render_api_card(ref: dict, dst: Path,
                     log: Callable[[str], None] | None = None) -> Path | None:
    """Fallback capture: compose a clean reference card (title + lead paragraph +
    optional thumbnail) from the resolved page's summary. Needs Pillow."""
    if not _PIL:
        _log(log, "Pillow не установлен — не могу нарисовать карточку-источник.")
        return None
    W, H = 1200, 900
    img = Image.new("RGB", (W, H), (247, 248, 250))
    draw = ImageDraw.Draw(img)

    def _font(size: int, bold: bool = False):
        for name in ((r"C:\Windows\Fonts\segoeuib.ttf",) if bold else ()) + (
            r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf",
        ):
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
        return ImageFont.load_default()

    pad = 64
    y = pad
    src_label = "Wikipedia" if ref.get("source") == "wikipedia" else "arXiv.org"
    draw.text((pad, y), src_label.upper(), font=_font(26, bold=True), fill=(90, 100, 115))
    y += 46
    title = ref.get("title", "") or ""
    tfont = _font(52, bold=True)
    for line in _wrap_text(draw, title, tfont, W - 2 * pad)[:3]:
        draw.text((pad, y), line, font=tfont, fill=(20, 24, 30))
        y += 62
    y += 18
    draw.line((pad, y, W - pad, y), fill=(210, 214, 220), width=2)
    y += 28

    # Optional lead image on the right.
    text_w = W - 2 * pad
    thumb = ref.get("thumbnail", "")
    if thumb:
        try:
            import io as _io
            th = Image.open(_io.BytesIO(_download(thumb, timeout=30))).convert("RGB")
            tw = 340
            th.thumbnail((tw, 420))
            img.paste(th, (W - pad - th.width, y))
            text_w = (W - pad - th.width - 32) - pad
        except Exception:
            pass

    body = ref.get("extract", "") or ""
    bfont = _font(30)
    for line in _wrap_text(draw, body, bfont, text_w)[:14]:
        draw.text((pad, y), line, font=bfont, fill=(55, 62, 72))
        y += 42
        if y > H - pad:
            break
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst)
    return dst if dst.exists() else None


def capture_reference(ref: dict, dst: Path, settings: Any,
                      log: Callable[[str], None] | None = None) -> Path | None:
    """Produce a still image of the reference: a real screenshot when Playwright
    is available, otherwise a rendered API card, otherwise the lead thumbnail.
    Returns None only if every path fails (caller then falls back to Ken Burns)."""
    url = ref.get("url", "")
    shot = _playwright_shot(url, dst, log=log) if url else None
    if shot:
        return shot
    card = _render_api_card(ref, dst, log=log)
    if card:
        return card
    thumb = ref.get("thumbnail", "")
    if thumb:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(_download(thumb, timeout=30))
            return dst
        except Exception:
            pass
    return None


# ─── composite ───────────────────────────────────────────────────────
def _build_panel_png(screenshot: Path, dst: Path, target_h: int = 860) -> Path | None:
    """Turn a raw screenshot into a framed panel PNG (rounded corners + drop
    shadow) so the slide-in reads as a designed insert, not a raw paste. Pillow
    only; returns None so the caller uses a plain rectangular panel instead."""
    if not _PIL:
        return None
    try:
        im = Image.open(screenshot).convert("RGB")
    except Exception:
        return None
    # Crop very tall screenshots to a pleasant aspect (keep the top / lead).
    max_ratio = 1.35  # height:width
    if im.height > im.width * max_ratio:
        im = im.crop((0, 0, im.width, int(im.width * max_ratio)))
    # Scale to target height (never upscale beyond ~1.5x).
    scale = min(target_h / im.height, 1.5)
    im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))

    radius = 24
    # Rounded-corner mask.
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width, im.height), radius=radius, fill=255)
    rounded = Image.new("RGBA", im.size, (0, 0, 0, 0))
    rounded.paste(im, (0, 0), mask)

    # Canvas with room for a soft drop shadow.
    m = 40
    canvas = Image.new("RGBA", (im.width + 2 * m, im.height + 2 * m), (0, 0, 0, 0))
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (m, m + 8, m + im.width, m + im.height + 8), radius=radius, fill=(0, 0, 0, 150)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    canvas = Image.alpha_composite(canvas, shadow)
    canvas.alpha_composite(rounded, (m, m))
    dst.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dst)
    return dst if dst.exists() else None


def _ffprobe_ok() -> bool:  # small hook for tests to stub
    return True


def build_reference_clip(screenshot: Path, dst: Path, ffmpeg: str,
                         slot_seconds: float, credit: str = "",
                         slide_seconds: float = 0.6,
                         log: Callable[[str], None] | None = None) -> Path:
    """Render a slot-length 1920x1080 clip: the screenshot (framed as a panel)
    slides in from the right with an ease-out over `slide_seconds` and holds,
    over a blurred, darkened copy of itself. A small credit is burned in
    bottom-left for attribution. Audio is dropped — export handles voice/music."""
    ff = ffmpeg or "ffmpeg"
    dur = max(0.8, float(slot_seconds) if slot_seconds and slot_seconds > 0 else 5.0)

    panel = _build_panel_png(screenshot, dst.with_suffix(".panel.png"))
    panel_input = panel if panel is not None else screenshot

    # Ease-out slide: offset = (start-rest) * (1 - t/T)^2, start = W (off-right),
    # rest = (W-w)/2. Symbolic in W/w/H/h/t so panel size need not be known here.
    T = max(0.2, float(slide_seconds))
    x_expr = (
        f"(W-w)/2+(W-(W-w)/2)*pow(max(0\\,1-t/{T:.2f})\\,2)"
    )
    # Panel already framed by Pillow; only clamp height when we fell back to raw.
    panel_chain = "[1:v]format=rgba[pnl]" if panel is not None else \
        "[1:v]scale=-2:'min(880\\,ih)':flags=lanczos,format=rgba[pnl]"

    graph = (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,gblur=sigma=20,eq=brightness=-0.28:saturation=0.82[bg];"
        f"{panel_chain};"
        f"[bg][pnl]overlay=x='{x_expr}':y='(H-h)/2':format=auto[ov]"
    )

    out_label = "[ov]"
    cred_file = None
    font = None
    try:
        from core.export import find_overlay_font, ffmpeg_filter_path
    except Exception:
        find_overlay_font = None  # type: ignore
        ffmpeg_filter_path = None  # type: ignore
    if credit and find_overlay_font is not None:
        font = find_overlay_font()
    if credit and font is not None:
        cred_file = dst.with_suffix(".credit.txt")
        cred_file.write_text(credit, encoding="utf-8")
        tf = ffmpeg_filter_path(cred_file)
        ff_font = ffmpeg_filter_path(Path(font))
        graph += (
            f";[ov]drawtext=fontfile='{ff_font}':textfile='{tf}':"
            "x=44:y=H-th-40:fontsize=30:fontcolor=white@0.92:"
            "box=1:boxcolor=black@0.45:boxborderw=14[out]"
        )
        out_label = "[out]"

    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ff, "-y",
        "-loop", "1", "-i", str(screenshot),
        "-loop", "1", "-i", str(panel_input),
        "-filter_complex", graph,
        "-map", out_label,
        "-t", f"{dur:.2f}", "-r", "25",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # Tidy the scratch files regardless of outcome.
    for scratch in (panel if panel is not None else None, cred_file):
        try:
            if scratch:
                Path(scratch).unlink(missing_ok=True)
        except Exception:
            pass
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(
            f"ffmpeg reference build failed: {proc.stderr[-400:] if proc.stderr else 'unknown error'}"
        )
    return dst


# ─── orchestration ───────────────────────────────────────────────────
def reference_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "reference_enable", True))


def fetch_reference_for_card(project_dir: Path, card: Any, settings: Any,
                             lang: str = "en",
                             log: Callable[[str], None] | None = None) -> dict | None:
    """Resolve -> capture -> build for one card. Returns the fields to write back,
    or None on any real miss (no page found / capture failed) so the caller can
    fall back to Ken Burns / AI. Never raises for a missing dependency."""
    if not reference_enabled(settings):
        _log(log, "Инсерт-источник отключён в настройках.")
        return None

    ref = resolve_reference(card, settings, lang=lang, log=log)
    if not ref:
        _log(log, "Источник: подходящая страница не найдена.")
        return None

    shot_index = int(getattr(card, "shot_index", 0) or 0)
    images_dir = project_dir / "assets" / "images"
    videos_dir = project_dir / "assets" / "videos"
    shot_png = images_dir / f"shot_{shot_index:03d}_reference.png"
    dst_mp4 = videos_dir / f"shot_{shot_index:03d}_reference.mp4"

    _log(log, f"Источник: снимаю страницу ({ref['source']})...")
    shot = capture_reference(ref, shot_png, settings, log=log)
    if not shot:
        _log(log, "Источник: не удалось получить изображение страницы.")
        return None

    ffmpeg = getattr(settings, "ffmpeg", "") or "ffmpeg"
    slot = _tc_to_seconds(getattr(card, "end", "")) - _tc_to_seconds(getattr(card, "start", ""))
    _log(log, f"Источник: собираю выезжающий инсерт под слот {slot:.0f}с...")
    build_reference_clip(shot, dst_mp4, ffmpeg, slot, credit=ref.get("credit", ""), log=log)

    return {
        "video_output": str(dst_mp4),
        "image_output": str(shot),
        "reference_url": ref.get("url", ""),
        "reference_source": ref.get("source", ""),
        "reference_title": ref.get("title", ""),
        "reference_credit": ref.get("credit", ""),
        "source_url": ref.get("url", ""),
    }
