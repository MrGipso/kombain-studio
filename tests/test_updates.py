import subprocess
from pathlib import Path

import pytest

from backend.routers import system
from core import updates
from core.config import Settings
from tools import frontend_build, self_update


# --- version ordering -----------------------------------------------------------

@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("1.0.0-rc.1", "1.0.0-rc.2"),
        ("1.0.0-rc.2", "1.0.0"),
        ("1.0.0-beta.3", "1.0.0-rc.1"),
        ("1.0.0", "1.0.1-rc.1"),
        ("v1.9.0", "1.10.0"),
        ("1.0.0rc2", "1.0.0"),
    ],
)
def test_versions_sort_like_releases(older, newer):
    assert updates.parse_version(older) < updates.parse_version(newer)


def test_unparseable_version_is_ignored():
    assert updates.parse_version("nightly") is None


RELEASES = [
    {"tag_name": "v1.0.0-rc.1", "prerelease": True, "html_url": "u-rc1"},
    {"tag_name": "v1.0.0", "prerelease": False, "html_url": "u-100"},
    {"tag_name": "v1.1.0-rc.1", "prerelease": True, "html_url": "u-110rc1"},
    {"tag_name": "v2.0.0", "draft": True, "html_url": "u-draft"},
    {"tag_name": "experiment", "prerelease": False},
]


def test_stable_install_is_never_offered_a_prerelease():
    assert updates.pick_latest(RELEASES, "1.0.0")["html_url"] == "u-100"


def test_rc_published_as_latest_is_still_a_prerelease():
    releases = [{"tag_name": "v1.0.0", "html_url": "u-100"}, {"tag_name": "v1.1.0-rc.1", "prerelease": False, "html_url": "u-rc"}]
    assert updates.pick_latest(releases, "1.0.0")["html_url"] == "u-100"


def test_prerelease_install_follows_prereleases():
    assert updates.pick_latest(RELEASES, "1.0.0-rc.1")["html_url"] == "u-110rc1"


def test_drafts_and_odd_tags_are_skipped():
    assert updates.pick_latest([RELEASES[3], RELEASES[4]], "1.0.0") is None


# --- check_for_updates -------------------------------------------------------------

class Fetcher:
    def __init__(self, releases=RELEASES, error=None):
        self.releases, self.error, self.calls = releases, error, 0

    def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.releases


def _check(tmp_path, fetch, *, now=1000.0, current="1.0.0-rc.1", **kwargs):
    return updates.check_for_updates(
        enabled=True, current=current, fetch=fetch, clock=lambda: now,
        cache_path=tmp_path / "cache.json", root=tmp_path, **kwargs,
    )


def test_newer_release_is_reported(tmp_path):
    status = _check(tmp_path, Fetcher())
    assert status.update_available is True
    assert status.latest == "1.1.0-rc.1"
    assert status.release_url == "u-110rc1"
    assert status.install == "archive"  # tmp_path has no .git


def test_up_to_date_install(tmp_path):
    status = _check(tmp_path, Fetcher(), current="1.1.0-rc.1")
    assert (status.update_available, status.latest) == (False, "1.1.0-rc.1")


def test_disabled_check_makes_no_request(tmp_path):
    fetch = Fetcher()
    status = updates.check_for_updates(enabled=False, fetch=fetch, cache_path=tmp_path / "c.json")
    assert fetch.calls == 0
    assert status.enabled is False and status.update_available is False


def test_answer_is_cached_for_a_day(tmp_path):
    fetch = Fetcher()
    _check(tmp_path, fetch, now=1000.0)
    _check(tmp_path, fetch, now=1000.0 + 23 * 3600)
    assert fetch.calls == 1
    _check(tmp_path, fetch, now=1000.0 + 25 * 3600)
    assert fetch.calls == 2


def test_force_ignores_the_cache(tmp_path):
    fetch = Fetcher()
    _check(tmp_path, fetch)
    _check(tmp_path, fetch, force=True)
    assert fetch.calls == 2


def test_cache_from_another_version_is_not_trusted(tmp_path):
    fetch = Fetcher()
    _check(tmp_path, fetch, current="1.0.0-rc.1")
    status = _check(tmp_path, fetch, current="1.1.0-rc.1")
    assert fetch.calls == 2
    assert status.update_available is False


def test_offline_is_reported_and_retried_after_an_hour(tmp_path):
    fetch = Fetcher(error=OSError("no network"))
    status = _check(tmp_path, fetch, now=1000.0)
    assert status.update_available is False
    assert "no network" in status.error
    _check(tmp_path, fetch, now=1000.0 + 1800)
    assert fetch.calls == 1
    _check(tmp_path, fetch, now=1000.0 + 3700)
    assert fetch.calls == 2


def test_git_checkout_is_detected(tmp_path):
    (tmp_path / ".git").mkdir()
    assert _check(tmp_path, Fetcher()).install == "git"


def test_endpoint_respects_the_setting(monkeypatch):
    calls = []
    monkeypatch.setattr(system, "load_settings", lambda: Settings(check_updates=False))
    monkeypatch.setattr(system, "check_for_updates", lambda **kw: calls.append(kw) or updates.UpdateStatus(enabled=kw["enabled"], current="x"))
    assert system.updates(force=True)["enabled"] is False
    assert calls == [{"enabled": False, "force": True}]


def test_check_updates_defaults_to_on():
    assert Settings().check_updates is True


# --- frontend build stamp ------------------------------------------------------------

def _frontend(tmp_path: Path) -> tuple[Path, Path]:
    frontend, static = tmp_path / "frontend", tmp_path / "static"
    (frontend / "src").mkdir(parents=True)
    (frontend / "node_modules").mkdir()
    (frontend / "src" / "App.tsx").write_text("export default 1\n", encoding="utf-8")
    (frontend / "package-lock.json").write_text("{}\n", encoding="utf-8")
    static.mkdir()
    return frontend, static


class Npm:
    def __init__(self, static: Path, fail: str | None = None):
        self.static, self.fail, self.commands = static, fail, []

    def __call__(self, command, cwd):
        self.commands.append(list(command[1:]))
        if self.fail and self.fail in command:
            return 1
        if "build" in command:
            (self.static / "index.html").write_text("<html>", encoding="utf-8")
        return 0


def _ensure(frontend, static, npm, **kwargs):
    return frontend_build.ensure_built(frontend=frontend, static=static, npm="npm", run=npm, log=lambda _m: None, **kwargs)


def test_first_run_installs_and_builds(tmp_path):
    frontend, static = _frontend(tmp_path)
    npm = Npm(static)
    assert _ensure(frontend, static, npm) is True
    assert npm.commands == [["ci"], ["run", "build"]]
    assert frontend_build.inspect(frontend, static).needs_build is False


def test_unchanged_sources_are_not_rebuilt(tmp_path):
    frontend, static = _frontend(tmp_path)
    _ensure(frontend, static, Npm(static))
    npm = Npm(static)
    assert _ensure(frontend, static, npm) is True
    assert npm.commands == []


def test_changed_source_is_rebuilt_without_reinstall(tmp_path):
    frontend, static = _frontend(tmp_path)
    _ensure(frontend, static, Npm(static))
    (frontend / "src" / "App.tsx").write_text("export default 2\n", encoding="utf-8")
    npm = Npm(static)
    _ensure(frontend, static, npm)
    assert npm.commands == [["run", "build"]]


def test_changed_lockfile_reinstalls_packages(tmp_path):
    frontend, static = _frontend(tmp_path)
    _ensure(frontend, static, Npm(static))
    (frontend / "package-lock.json").write_text('{"v": 2}\n', encoding="utf-8")
    npm = Npm(static)
    _ensure(frontend, static, npm)
    assert npm.commands == [["ci"], ["run", "build"]]


def test_line_endings_do_not_count_as_a_change(tmp_path):
    frontend, static = _frontend(tmp_path)
    before = frontend_build.source_fingerprint(frontend)
    (frontend / "src" / "App.tsx").write_bytes(b"export default 1\r\n")
    assert frontend_build.source_fingerprint(frontend) == before


def test_old_build_is_kept_when_npm_is_missing(tmp_path, monkeypatch):
    frontend, static = _frontend(tmp_path)
    (static / "index.html").write_text("<old>", encoding="utf-8")
    monkeypatch.setattr(frontend_build, "_npm", lambda: None)
    assert frontend_build.ensure_built(frontend=frontend, static=static, log=lambda _m: None) is True
    (static / "index.html").unlink()
    assert frontend_build.ensure_built(frontend=frontend, static=static, log=lambda _m: None) is False


def test_failed_build_reports_whether_anything_is_left(tmp_path):
    frontend, static = _frontend(tmp_path)
    assert _ensure(frontend, static, Npm(static, fail="build")) is False
    assert not (static / frontend_build.STAMP_NAME).exists()


# --- self-update ---------------------------------------------------------------------

class FakeGit:
    def __init__(self, *, branch=True, dirty="", pull_ok=True, moves=True):
        self.branch, self.dirty, self.pull_ok, self.moves = branch, dirty, pull_ok, moves
        self.head, self.calls = "aaa", []

    def __call__(self, args):
        self.calls.append(args[0])
        ok = subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "symbolic-ref":
            if not self.branch:
                return subprocess.CompletedProcess(args, 1, "", "")
            return subprocess.CompletedProcess(args, 0, "master\n", "")
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 0, self.dirty, "")
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, self.head + "\n", "")
        if args[0] == "pull":
            if not self.pull_ok:
                return subprocess.CompletedProcess(args, 1, "", "fatal: Not possible to fast-forward")
            if self.moves:
                self.head = "bbb"
            return ok
        raise AssertionError(args)


def _checkout(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "version.py").write_text('__version__ = "1.0.0-rc.2"\n', encoding="utf-8")
    return tmp_path


def _self_update(root, git, *, running=False):
    messages = []
    code = self_update.update(root=root, git=git, has_git=lambda: True, running=lambda: running, log=messages.append)
    return code, "\n".join(messages)


def test_archive_install_gets_manual_instructions(tmp_path):
    code, text = _self_update(tmp_path, FakeGit())
    assert code == 1 and "releases" in text


def test_running_app_blocks_the_update(tmp_path):
    git = FakeGit()
    code, text = _self_update(_checkout(tmp_path), git, running=True)
    assert code == 1 and "Закройте" in text
    assert git.calls == []


def test_local_changes_are_never_overwritten(tmp_path):
    git = FakeGit(dirty=" M core/lm.py\n")
    code, text = _self_update(_checkout(tmp_path), git)
    assert code == 1 and "core/lm.py" in text
    assert "pull" not in git.calls


def test_detached_head_is_explained(tmp_path):
    git = FakeGit(branch=False)
    code, text = _self_update(_checkout(tmp_path), git)
    assert code == 1 and "git checkout master" in text


def test_successful_pull(tmp_path):
    code, text = _self_update(_checkout(tmp_path), FakeGit())
    assert code == 0 and "обновлён" in text


def test_nothing_to_pull(tmp_path):
    code, text = _self_update(_checkout(tmp_path), FakeGit(moves=False))
    assert code == 0 and "Обновлений нет" in text


def test_failed_pull_is_reported(tmp_path):
    code, text = _self_update(_checkout(tmp_path), FakeGit(pull_ok=False))
    assert code == 1 and "fast-forward" in text
