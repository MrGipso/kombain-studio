"""Update a git checkout of Комбайн Studio to the latest code of its branch.

Called by ``update.bat`` / ``update.sh``. Only the git part lives here; the
wrappers then run ``uv sync`` and rebuild the web UI, after this process has
exited (on Windows a running interpreter keeps files of the environment busy).

Exit codes: 0 updated or already current, 1 cannot update (the message says
why and what to do instead).
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parent.parent
RELEASES_URL = "https://github.com/MrGipso/kombain-studio/releases"

Git = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _git(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace",
    )


def read_version(root: Path = ROOT) -> str:
    text = (root / "core" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "?"


def app_is_running(host: str | None = None, port: int | None = None) -> bool:
    host = host or os.environ.get("PIPELINE_HOST", "127.0.0.1")
    port = port or int(os.environ.get("PIPELINE_PORT", "8000"))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


MANUAL_UPDATE = (
    "Эта папка установлена не через git, поэтому обновить её автоматически нельзя.\n"
    f"  1. Скачайте новую версию: {RELEASES_URL}\n"
    "  2. Распакуйте её в новую папку.\n"
    "  3. Перенесите папку projects из старой установки (если проекты лежат там).\n"
    "Настройки хранятся отдельно от папки программы и сохранятся сами."
)


def update(
    *,
    root: Path = ROOT,
    git: Git = _git,
    has_git: Callable[[], bool] = lambda: shutil.which("git") is not None,
    running: Callable[[], bool] = app_is_running,
    log: Callable[[str], None] = print,
) -> int:
    if not (root / ".git").exists():
        log(MANUAL_UPDATE)
        return 1
    if not has_git():
        log("git не найден. Установите Git (https://git-scm.com) и запустите обновление снова.\n\n" + MANUAL_UPDATE)
        return 1
    if running():
        log("Комбайн Studio сейчас запущен. Закройте окно приложения (или сервер) и запустите обновление снова.")
        return 1

    branch = git(["symbolic-ref", "--quiet", "--short", "HEAD"])
    if branch.returncode != 0:
        log("Сейчас выбрана конкретная версия, а не ветка. Переключитесь на основную ветку:\n"
            "  git checkout master\nи запустите обновление снова.")
        return 1
    changed = git(["status", "--porcelain", "--untracked-files=no"])
    if changed.stdout.strip():
        log("В файлах программы есть локальные изменения — обновление их не трогает:\n"
            + changed.stdout.rstrip()
            + "\nСохраните их (git stash) или верните как было (git checkout -- .) и запустите обновление снова.")
        return 1

    before = git(["rev-parse", "HEAD"]).stdout.strip()
    old_version = read_version(root)
    log(f"Текущая версия: {old_version}. Получаю обновления…")
    pulled = git(["pull", "--ff-only", "--tags"])
    if pulled.returncode != 0:
        log("Не удалось получить обновления:\n" + (pulled.stderr or pulled.stdout).strip())
        return 1
    after = git(["rev-parse", "HEAD"]).stdout.strip()
    if after == before:
        log(f"Обновлений нет: установлена последняя версия ветки {branch.stdout.strip()} ({old_version}).")
        return 0
    new_version = read_version(root)
    if new_version == old_version:
        log(f"Код обновлён до последнего в ветке {branch.stdout.strip()} (версия {new_version}).")
    else:
        log(f"Обновлено: {old_version} → {new_version}.")
    return 0


def main() -> int:
    return update()


if __name__ == "__main__":
    sys.exit(main())
