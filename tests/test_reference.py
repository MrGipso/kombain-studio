"""Offline tests for the reference-insert route (core/reference.py).

No network, no ffmpeg, no Playwright, no Pillow required: HTTP and subprocess
are monkeypatched so the resolve/build logic is exercised deterministically.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.reference as R
from core import process


# ─── helpers ─────────────────────────────────────────────────────────
class Card:
    def __init__(self, **kw):
        self.shot_index = kw.get("shot_index", 1)
        self.start = kw.get("start", "00:00:00")
        self.end = kw.get("end", "00:00:05")
        self.reference_query = kw.get("reference_query", "")
        self.reference_source = kw.get("reference_source", "wikipedia")
        self.asset_query = kw.get("asset_query", "")


class Settings:
    def __init__(self, enable=True, ffmpeg="ffmpeg"):
        self.reference_enable = enable
        self.ffmpeg = ffmpeg


# ─── query / lang / timecode ─────────────────────────────────────────
def test_clean_query_strips_quotes_and_space():
    assert R._clean_query("  «Attention Is All You Need» ") == "Attention Is All You Need"
    assert R._clean_query('"transformer"') == "transformer"
    assert R._clean_query("  a   b ") == "a b"


def test_wiki_lang_mapping():
    assert R.wiki_lang("ru") == "ru"
    assert R.wiki_lang("RU") == "ru"
    assert R.wiki_lang("pt-BR") == "pt"
    assert R.wiki_lang("xx") == "en"
    assert R.wiki_lang("") == "en"


def test_timecode():
    assert R._tc_to_seconds("00:01:05") == 65
    assert R._tc_to_seconds("1:00") == 60
    assert R._tc_to_seconds("garbage") == 0.0


def test_reference_enabled():
    assert R.reference_enabled(Settings(enable=True)) is True
    assert R.reference_enabled(Settings(enable=False)) is False


# ─── Wikipedia resolver (mocked HTTP) ────────────────────────────────
def _fake_get_json_factory(has_ru=True):
    def fake(url, timeout=15):
        if "opensearch" in url:
            if "ru.wikipedia" in url and not has_ru:
                return ["q", [], [], []]
            title = "Трансформер" if "ru.wikipedia" in url else "Transformer (deep learning)"
            return ["q", [title], [""], [f"https://x/wiki/{title}"]]
        # REST summary
        is_ru = "ru.wikipedia" in url
        return {
            "title": "Трансформер" if is_ru else "Transformer (deep learning)",
            "extract": "Модель…" if is_ru else "A model…",
            "thumbnail": {"source": "https://img/thumb.png"},
            "content_urls": {"desktop": {"page": "https://x/wiki/page"}},
        }
    return fake


def test_resolve_wikipedia_direct(monkeypatch):
    monkeypatch.setattr(R, "_get_json", _fake_get_json_factory(has_ru=True))
    hit = R.resolve_wikipedia("трансформер", lang="ru")
    assert hit and hit["source"] == "wikipedia"
    assert hit["url"] == "https://x/wiki/page"
    assert hit["lang"] == "ru"
    assert "CC BY-SA" in hit["credit"]


def test_resolve_wikipedia_en_fallback(monkeypatch):
    # No RU article -> falls back to English wiki.
    monkeypatch.setattr(R, "_get_json", _fake_get_json_factory(has_ru=False))
    hit = R.resolve_wikipedia("transformer", lang="ru")
    assert hit and hit["lang"] == "en"
    assert "Transformer" in hit["title"]


def test_resolve_wikipedia_empty_query():
    assert R.resolve_wikipedia("", lang="en") is None


def test_title_relevant_guard():
    # Real fuzzy-mismatch cases observed in the wild.
    assert R._title_relevant("LM Studio", "StudioCanal") is False
    assert R._title_relevant("llama.cpp", "Llama") is False       # 1/2 tokens < 0.6
    assert R._title_relevant("Ollama", "Llama") is False
    # Legitimate matches.
    assert R._title_relevant("Трансформер", "Трансформер (модель)") is True
    assert R._title_relevant("Transformer deep learning",
                             "Transformer (deep learning architecture)") is True
    assert R._title_relevant("Python", "Python (programming language)") is True


def test_resolve_wikipedia_rejects_fuzzy_mismatch(monkeypatch):
    # opensearch returns 'StudioCanal' for 'LM Studio' -> must be rejected, and
    # the English fallback (also a mismatch here) too -> overall None.
    def fake(url, timeout=15):
        if "opensearch" in url:
            return ["LM Studio", ["StudioCanal"], [""], ["https://x"]]
        return {"title": "StudioCanal", "extract": "", "content_urls": {"desktop": {"page": "u"}}}
    monkeypatch.setattr(R, "_get_json", fake)
    assert R.resolve_wikipedia("LM Studio", lang="ru") is None


def test_resolve_reference_arxiv_falls_back_to_wiki(monkeypatch):
    monkeypatch.setattr(R, "resolve_arxiv", lambda q, log=None: None)
    marker = {"source": "wikipedia", "title": "T", "url": "u", "credit": "c"}
    monkeypatch.setattr(R, "resolve_wikipedia", lambda q, lang="en", log=None: marker)
    card = Card(reference_query="Attention Is All You Need", reference_source="arxiv")
    assert R.resolve_reference(card, Settings(), lang="ru") is marker


# ─── panel + clip (mocked subprocess) ────────────────────────────────
def test_build_reference_clip_command(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        # Simulate ffmpeg writing the output so the function returns cleanly.
        Path(cmd[-1]).write_bytes(b"\x00")
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(process.subprocess, "run", fake_run)
    # Force the no-Pillow path so the panel chain scales the raw screenshot.
    monkeypatch.setattr(R, "_build_panel_png", lambda *a, **k: None)

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n")
    dst = tmp_path / "out.mp4"
    out = R.build_reference_clip(shot, dst, "ffmpeg", slot_seconds=6, credit="")
    assert out == dst and dst.exists()
    cmd = captured["cmd"]
    # Slot length honoured, filtergraph present, eased slide-in expression present.
    assert "-t" in cmd and "6.00" in cmd
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "overlay=x=" in fc and "gblur" in fc
    assert "pow(max(0" in fc  # ease-out slide


def test_fetch_reference_disabled_returns_none():
    assert R.fetch_reference_for_card(Path("."), Card(), Settings(enable=False)) is None
