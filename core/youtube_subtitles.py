"""Restricted yt-dlp adapter used by the Script/Rewriter intake flow."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ALLOWED_YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"
})


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SUPPORTED_COOKIE_BROWSERS = frozenset({
    "", "edge", "chrome", "firefox", "brave", "opera", "vivaldi",
})


class SubtitleImportError(RuntimeError):
    def __init__(self, message: str, *, code: str = "youtube_error", http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class SubtitleDownload:
    path: Path
    language: str
    kind: str
    used_fallback: bool = False


def validate_youtube_url(url: str) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in ALLOWED_YOUTUBE_HOSTS:
        raise ValueError("Разрешены только ссылки youtube.com и youtu.be")
    if parsed.username or parsed.password:
        raise ValueError("Ссылка не должна содержать данные авторизации")
    return value


def _yt_dlp_module():
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:
        raise SubtitleImportError(
            "yt-dlp не установлен. Установите зависимости Комбайн Studio и повторите попытку."
        ) from exc
    return yt_dlp


def _clean_error(error: BaseException | str) -> str:
    text = ANSI_ESCAPE_RE.sub("", str(error or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _friendly_error(error: BaseException | str, *, operation: str) -> SubtitleImportError:
    detail = _clean_error(error)
    lowered = detail.casefold()
    if "http error 429" in lowered or "too many requests" in lowered:
        return SubtitleImportError(
            "YouTube временно ограничил запросы (HTTP 429). Не повторяйте импорт подряд: "
            "подождите и попробуйте позже либо выберите cookies браузера в Настройки → YouTube.",
            code="youtube_rate_limited",
            http_status=429,
        )
    if any(marker in lowered for marker in (
        "sign in to confirm", "cookies", "not a bot", "confirm you’re not a bot",
        "confirm you're not a bot",
    )):
        return SubtitleImportError(
            "YouTube запросил авторизацию. Выберите браузер с активной сессией YouTube "
            "в Настройки → YouTube и повторите попытку.",
            code="youtube_auth_required",
            http_status=403,
        )
    prefix = "Не удалось получить сведения о видео" if operation == "inspect" else "Не удалось скачать дорожку субтитров"
    return SubtitleImportError(f"{prefix}: {detail}", code="youtube_download_failed")


def _retry_sleep(attempt: int) -> float:
    return float(min(10, 2 ** max(0, int(attempt))))


def _base_options(
    *,
    cookies_browser: str = "",
    retry_count: int = 3,
    request_delay_sec: float = 1.0,
) -> dict[str, Any]:
    browser = str(cookies_browser or "").strip().lower()
    if browser not in SUPPORTED_COOKIE_BROWSERS:
        raise ValueError("Неподдерживаемый браузер для cookies YouTube")
    retries = max(0, min(5, int(retry_count)))
    delay = max(0.0, min(10.0, float(request_delay_sec)))
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # Subtitle-only intake must not depend on yt-dlp finding a playable
        # video format. Authenticated YouTube sessions can expose captions and
        # metadata while withholding regular media formats; without this flag
        # extract_info() fails before we can inspect or download the captions.
        "ignore_no_formats_error": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": retries,
        "fragment_retries": retries,
        "extractor_retries": retries,
        "sleep_interval_requests": delay,
        "retry_sleep_functions": {
            "http": _retry_sleep,
            "fragment": _retry_sleep,
            "extractor": _retry_sleep,
        },
    }
    if browser:
        options["cookiesfrombrowser"] = (browser,)
    return options


def _original_language(info: dict[str, Any], tracks: list[dict[str, Any]]) -> str:
    direct = str(info.get("language") or info.get("original_language") or "").strip()
    available = {str(track.get("language") or "") for track in tracks}
    if direct in available:
        return direct
    for language in available:
        if language.endswith("-orig"):
            return language
    manual = [track["language"] for track in tracks if track.get("kind") == "manual"]
    return str(manual[0]) if len(manual) == 1 else ""


def inspect_subtitles(
    url: str,
    *,
    cookies_browser: str = "",
    retry_count: int = 3,
    request_delay_sec: float = 1.0,
) -> dict[str, Any]:
    safe_url = validate_youtube_url(url)
    options = _base_options(
        cookies_browser=cookies_browser,
        retry_count=retry_count,
        request_delay_sec=request_delay_sec,
    )
    yt_dlp = _yt_dlp_module()
    options["extract_flat"] = False
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(safe_url, download=False)
    except Exception as exc:
        raise _friendly_error(exc, operation="inspect") from exc
    if not isinstance(info, dict):
        raise SubtitleImportError("yt-dlp не вернул сведения о видео")

    tracks: list[dict[str, Any]] = []
    for kind, field in (("manual", "subtitles"), ("auto", "automatic_captions")):
        source = info.get(field) if isinstance(info.get(field), dict) else {}
        for language, formats in source.items():
            available_formats = sorted({
                str(item.get("ext", ""))
                for item in formats if isinstance(item, dict) and item.get("ext")
            })
            tracks.append({"language": str(language), "kind": kind, "formats": available_formats})
    tracks.sort(key=lambda item: (item["kind"] != "manual", item["language"]))
    original_language = _original_language(info, tracks)
    for track in tracks:
        track["is_original"] = bool(original_language and track["language"] == original_language)
    return {
        "id": str(info.get("id", "")),
        "title": str(info.get("title", "")),
        "channel": str(info.get("channel") or info.get("uploader") or ""),
        "duration": int(info.get("duration") or 0),
        "webpage_url": str(info.get("webpage_url") or safe_url),
        "original_language": original_language,
        "tracks": tracks,
    }


def download_subtitle(
    url: str,
    language: str,
    kind: str,
    destination: Path,
    *,
    fallback_language: str = "",
    fallback_kind: str = "",
    cookies_browser: str = "",
    retry_count: int = 3,
    request_delay_sec: float = 1.0,
) -> SubtitleDownload:
    safe_url = validate_youtube_url(url)
    if kind not in {"manual", "auto"}:
        raise ValueError("Неизвестный тип субтитров")
    safe_language = str(language or "").strip()
    if not safe_language or len(safe_language) > 40 or any(ch in safe_language for ch in "/\\\0"):
        raise ValueError("Некорректный язык субтитров")
    _base_options(
        cookies_browser=cookies_browser,
        retry_count=retry_count,
        request_delay_sec=request_delay_sec,
    )
    yt_dlp = _yt_dlp_module()

    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".yt-sub-", dir=str(destination)) as temporary:
        temporary_path = Path(temporary)
        safe_fallback = str(fallback_language or "").strip()
        if len(safe_fallback) > 40 or any(ch in safe_fallback for ch in "/\\\0"):
            safe_fallback = ""
        safe_fallback_kind = str(fallback_kind or kind).strip()
        if safe_fallback_kind not in {"manual", "auto"}:
            safe_fallback_kind = kind
        tracks = [(safe_language, kind)]
        if safe_fallback and (
            safe_fallback != safe_language or safe_fallback_kind != kind
        ):
            tracks.append((safe_fallback, safe_fallback_kind))
        last_error: Exception | None = None
        for index, (active_language, active_kind) in enumerate(tracks):
            options = _base_options(
                cookies_browser=cookies_browser,
                retry_count=retry_count,
                request_delay_sec=request_delay_sec,
            )
            options.update({
                "writesubtitles": active_kind == "manual",
                "writeautomaticsub": active_kind == "auto",
                "subtitleslangs": [active_language],
                "subtitlesformat": "vtt/srt/best",
                "sleep_interval_subtitles": max(0.0, min(10.0, float(request_delay_sec))),
                "outtmpl": str(temporary_path / f"source-{index}.%(ext)s"),
            })
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    ydl.download([safe_url])
            except Exception as exc:
                friendly = _friendly_error(exc, operation="download")
                # A rate-limit applies to the IP/session, not just one language.
                # Trying fallback tracks here only sends more blocked requests.
                if friendly.code in {"youtube_rate_limited", "youtube_auth_required"}:
                    raise friendly from exc
                last_error = exc
                continue
            candidates = sorted(temporary_path.glob(f"source-{index}.*"))
            candidates = [
                candidate for candidate in candidates
                if candidate.suffix.lower() in {".vtt", ".srt", ".ttml", ".srv1", ".srv2", ".srv3", ".json3"}
            ]
            if not candidates:
                last_error = RuntimeError("выбранная дорожка не была загружена")
                continue
            source = candidates[0]
            target = destination / f"source{source.suffix.lower()}"
            target.write_bytes(source.read_bytes())
            return SubtitleDownload(
                path=target,
                language=active_language,
                kind=active_kind,
                used_fallback=index > 0,
            )
        raise _friendly_error(last_error or "выбранная дорожка не была загружена", operation="download")
