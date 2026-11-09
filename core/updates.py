"""Check GitHub for a newer release of Комбайн Studio.

One anonymous request to the public GitHub API, at most once a day (errors
are retried after an hour). Nothing about the user or their projects is sent:
the request carries only a generic User-Agent with the app version. The check
can be switched off with the ``check_updates`` setting.

Pre-releases are offered only to users who already run a pre-release, so a
stable install is never pushed to a release candidate.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from core.version import __version__

REPOSITORY = "MrGipso/kombain-studio"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=20"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases"
ROOT = Path(__file__).resolve().parent.parent

CHECK_INTERVAL_SECONDS = 24 * 3600
RETRY_AFTER_ERROR_SECONDS = 3600
TIMEOUT_SECONDS = 5.0
CACHE_NAME = ".update-check.json"

_VERSION_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:[-.]?(a|alpha|b|beta|rc)\.?(\d+))?$", re.IGNORECASE,
)
_STAGE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2}
_FINAL = 3


def parse_version(text: str) -> tuple[int, int, int, int, int] | None:
    """``1.0.0-rc.2`` → ``(1, 0, 0, 2, 2)``; a final release sorts after its RCs."""
    match = _VERSION_RE.match((text or "").strip())
    if not match:
        return None
    major, minor, patch, stage, number = match.groups()
    rank = _STAGE_RANK[stage.lower()] if stage else _FINAL
    return int(major), int(minor), int(patch), rank, int(number or 0)


def is_prerelease(version: str) -> bool:
    parsed = parse_version(version)
    return parsed is not None and parsed[3] != _FINAL


@dataclass
class UpdateStatus:
    enabled: bool
    current: str
    latest: str = ""
    update_available: bool = False
    release_url: str = RELEASES_PAGE
    published_at: str = ""
    checked_at: float = 0.0
    error: str = ""
    # "git": update with update.bat / update.sh; "installer": run the new
    # installer over this one; "archive": download a new copy.
    install: str = "archive"
    platform: str = sys.platform

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def pick_latest(releases: list[dict], current: str) -> dict | None:
    """The newest published release this install may be offered."""
    allow_pre = is_prerelease(current)
    best: tuple[tuple, dict] | None = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        if release.get("prerelease") and not allow_pre:
            continue
        parsed = parse_version(str(release.get("tag_name") or ""))
        if parsed is None:
            continue
        if best is None or parsed > best[0]:
            best = (parsed, release)
    return best[1] if best else None


def _fetch_releases(url: str = RELEASES_API) -> list[dict]:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"KombainStudio/{__version__} (update check)",
    })
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("unexpected GitHub API answer")
    return data


def _data_dir() -> Path:
    configured = os.environ.get("PIPELINE_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    from core.config import DEFAULT_SETTINGS_PATH

    return DEFAULT_SETTINGS_PATH.parent


def _install_kind(root: Path = ROOT) -> str:
    if getattr(sys, "frozen", False):
        return "installer"
    return "git" if (root / ".git").exists() else "archive"


def _read_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # the cache is an optimisation; failing to write it is harmless


def check_for_updates(
    *,
    enabled: bool,
    force: bool = False,
    current: str = __version__,
    fetch: Callable[[], list[dict]] = _fetch_releases,
    clock: Callable[[], float] = time.time,
    cache_path: Path | None = None,
    root: Path = ROOT,
) -> UpdateStatus:
    status = UpdateStatus(enabled=enabled, current=current, install=_install_kind(root))
    if not enabled:
        return status

    cache_path = cache_path or (_data_dir() / CACHE_NAME)
    now = clock()
    cached = _read_cache(cache_path)
    fresh_for = RETRY_AFTER_ERROR_SECONDS if cached.get("error") else CHECK_INTERVAL_SECONDS
    # A cache written by another version of the app says nothing about this one.
    usable = cached.get("current") == current and 0 <= now - float(cached.get("checked_at") or 0) < fresh_for
    if usable and not force:
        latest, url, published, error = (
            cached.get("latest", ""), cached.get("release_url", RELEASES_PAGE),
            cached.get("published_at", ""), cached.get("error", ""),
        )
        checked_at = float(cached.get("checked_at") or 0)
    else:
        latest, url, published, error = "", RELEASES_PAGE, "", ""
        try:
            release = pick_latest(fetch(), current)
        except Exception as exc:  # noqa: BLE001 - offline or GitHub down: report, never fail
            error = f"Не удалось проверить обновления: {exc}"
            release = None
        if release is not None:
            latest = str(release.get("tag_name") or "").lstrip("vV")
            url = str(release.get("html_url") or RELEASES_PAGE)
            published = str(release.get("published_at") or "")
        checked_at = now
        _write_cache(cache_path, {
            "current": current, "latest": latest, "release_url": url,
            "published_at": published, "error": error, "checked_at": checked_at,
        })

    status.latest = latest
    status.release_url = url
    status.published_at = published
    status.error = error
    status.checked_at = checked_at
    newest, mine = parse_version(latest), parse_version(current)
    status.update_available = bool(newest and mine and newest > mine)
    return status


__all__ = ["UpdateStatus", "check_for_updates", "is_prerelease", "parse_version", "pick_latest"]
