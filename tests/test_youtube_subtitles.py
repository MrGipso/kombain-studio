from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import asyncio

import pytest
from fastapi import HTTPException

from backend.routers import script as script_router
from core import youtube_subtitles


class _FakeYoutubeDL:
    def __init__(self, options, *, info=None, download=None):
        self.options = options
        self._info = info
        self._download = download

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url, download=False):
        assert download is False
        if isinstance(self._info, BaseException):
            raise self._info
        return self._info

    def download(self, urls):
        if self._download:
            return self._download(self.options, urls)
        return 0


def test_inspection_applies_cookies_retries_delay_and_marks_original(monkeypatch):
    seen = []
    info = {
        "id": "video",
        "title": "Title",
        "language": "en",
        "subtitles": {},
        "automatic_captions": {
            "en": [{"ext": "vtt"}],
            "ru": [{"ext": "vtt"}],
        },
    }

    def factory(options):
        seen.append(options)
        return _FakeYoutubeDL(options, info=info)

    monkeypatch.setattr(
        youtube_subtitles,
        "_yt_dlp_module",
        lambda: SimpleNamespace(YoutubeDL=factory),
    )

    result = youtube_subtitles.inspect_subtitles(
        "https://www.youtube.com/watch?v=video",
        cookies_browser="edge",
        retry_count=4,
        request_delay_sec=1.5,
    )

    assert seen[0]["cookiesfrombrowser"] == ("edge",)
    assert seen[0]["retries"] == 4
    assert seen[0]["extractor_retries"] == 4
    assert seen[0]["sleep_interval_requests"] == 1.5
    assert seen[0]["skip_download"] is True
    assert seen[0]["ignore_no_formats_error"] is True
    assert result["original_language"] == "en"
    assert next(track for track in result["tracks"] if track["language"] == "en")["is_original"] is True
    assert next(track for track in result["tracks"] if track["language"] == "ru")["is_original"] is False


def test_download_falls_back_to_original_track(tmp_path, monkeypatch):
    attempts = []

    def download(options, _urls):
        language = options["subtitleslangs"][0]
        attempts.append((language, options["writeautomaticsub"], options["writesubtitles"]))
        if language == "ru":
            raise RuntimeError("requested subtitles are unavailable")
        output = Path(options["outtmpl"].replace("%(ext)s", "vtt"))
        output.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nText", encoding="utf-8")
        return 0

    monkeypatch.setattr(
        youtube_subtitles,
        "_yt_dlp_module",
        lambda: SimpleNamespace(
            YoutubeDL=lambda options: _FakeYoutubeDL(options, download=download)
        ),
    )

    result = youtube_subtitles.download_subtitle(
        "https://youtu.be/video",
        "ru",
        "auto",
        tmp_path,
        fallback_language="en",
        fallback_kind="manual",
        retry_count=2,
        request_delay_sec=0.5,
    )

    assert attempts == [("ru", True, False), ("en", False, True)]
    assert result.path.read_text(encoding="utf-8").startswith("WEBVTT")
    assert result.language == "en"
    assert result.kind == "manual"
    assert result.used_fallback is True


def test_rate_limit_is_clean_and_does_not_trigger_fallback(tmp_path, monkeypatch):
    attempts = []

    def download(options, _urls):
        attempts.append(options["subtitleslangs"][0])
        raise RuntimeError("\x1b[0;31mERROR:\x1b[0m HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(
        youtube_subtitles,
        "_yt_dlp_module",
        lambda: SimpleNamespace(
            YoutubeDL=lambda options: _FakeYoutubeDL(options, download=download)
        ),
    )

    with pytest.raises(youtube_subtitles.SubtitleImportError) as captured:
        youtube_subtitles.download_subtitle(
            "https://youtu.be/video",
            "ru",
            "auto",
            tmp_path,
            fallback_language="en",
            fallback_kind="auto",
        )

    assert captured.value.code == "youtube_rate_limited"
    assert captured.value.http_status == 429
    assert "\x1b" not in str(captured.value)
    assert "HTTP 429" in str(captured.value)
    assert attempts == ["ru"]


def test_invalid_cookie_browser_is_rejected_before_network(monkeypatch):
    monkeypatch.setattr(
        youtube_subtitles,
        "_yt_dlp_module",
        lambda: pytest.fail("network adapter must not be created"),
    )

    with pytest.raises(ValueError, match="Неподдерживаемый браузер"):
        youtube_subtitles.inspect_subtitles(
            "https://youtu.be/video",
            cookies_browser="unknown",
        )


def test_script_api_preserves_youtube_rate_limit_status(monkeypatch):
    monkeypatch.setattr(script_router, "_resolve_project", lambda _project_id: object())
    monkeypatch.setattr(
        script_router,
        "load_settings",
        lambda: SimpleNamespace(
            youtube_cookies_browser="edge",
            youtube_retry_count=3,
            youtube_request_delay_sec=1.0,
        ),
    )

    def fail(_url, **options):
        assert options["cookies_browser"] == "edge"
        raise youtube_subtitles.SubtitleImportError(
            "YouTube временно ограничил запросы (HTTP 429).",
            code="youtube_rate_limited",
            http_status=429,
        )

    monkeypatch.setattr(script_router, "inspect_subtitles", fail)

    with pytest.raises(HTTPException) as captured:
        asyncio.run(
            script_router.inspect_youtube(
                "demo",
                script_router.YoutubeInspectRequest(url="https://youtu.be/video"),
            )
        )

    assert captured.value.status_code == 429
    assert "HTTP 429" in captured.value.detail
