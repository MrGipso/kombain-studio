"""Keep the built web UI (``backend/static``) in step with ``frontend/`` sources.

Launchers used to build the UI only when ``backend/static/index.html`` was
missing, so after ``git pull`` or unpacking a newer release over an old one the
app kept serving the previous interface. Every build now leaves a stamp with a
fingerprint of the sources; a launcher rebuilds when the stamp no longer
matches, and reinstalls npm packages when the lockfile changed.

Usage:
    python tools/frontend_build.py --if-stale   # rebuild only when sources changed
    python tools/frontend_build.py              # always rebuild
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
STATIC = ROOT / "backend" / "static"
STAMP_NAME = ".build-stamp.json"

# Everything that changes the build output. node_modules and the output folder
# are deliberately not part of it.
SOURCE_DIRS = ("src", "public")
SOURCE_FILES = (
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.node.json",
)
LOCKFILE = "package-lock.json"


def _hash_files(frontend: Path, names: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in names:
        digest.update(path.relative_to(frontend).as_posix().encode("utf-8"))
        digest.update(b"\0")
        # Git on Windows may check text files out with CRLF; the build output
        # does not depend on line endings, so neither does the fingerprint.
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def source_files(frontend: Path = FRONTEND) -> list[Path]:
    files = [frontend / name for name in SOURCE_FILES if (frontend / name).is_file()]
    for folder in SOURCE_DIRS:
        root = frontend / folder
        if root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return sorted(files, key=lambda path: path.relative_to(frontend).as_posix())


def source_fingerprint(frontend: Path = FRONTEND) -> str:
    return _hash_files(frontend, source_files(frontend))


def lockfile_fingerprint(frontend: Path = FRONTEND) -> str:
    lock = frontend / LOCKFILE
    return _hash_files(frontend, [lock]) if lock.is_file() else ""


def read_stamp(static: Path = STATIC) -> dict:
    try:
        data = json.loads((static / STAMP_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class BuildState:
    built: bool
    stale: bool
    needs_install: bool

    @property
    def needs_build(self) -> bool:
        return not self.built or self.stale


def inspect(frontend: Path = FRONTEND, static: Path = STATIC) -> BuildState:
    stamp = read_stamp(static)
    built = (static / "index.html").is_file()
    stale = stamp.get("sources") != source_fingerprint(frontend)
    needs_install = (
        not (frontend / "node_modules").is_dir()
        or stamp.get("lockfile") != lockfile_fingerprint(frontend)
    )
    return BuildState(built=built, stale=stale, needs_install=needs_install)


def write_stamp(frontend: Path = FRONTEND, static: Path = STATIC) -> None:
    payload = {
        "sources": source_fingerprint(frontend),
        "lockfile": lockfile_fingerprint(frontend),
    }
    (static / STAMP_NAME).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _npm() -> str | None:
    # On Windows npm is npm.cmd; shutil.which resolves it through PATHEXT.
    return shutil.which("npm")


Runner = Callable[[Sequence[str], Path], int]


def _run(command: Sequence[str], cwd: Path) -> int:
    print(f"> {' '.join(command)}", flush=True)
    return subprocess.run(list(command), cwd=cwd, check=False).returncode


def ensure_built(
    *,
    force: bool = False,
    frontend: Path = FRONTEND,
    static: Path = STATIC,
    npm: str | None = None,
    run: Runner = _run,
    log: Callable[[str], None] = print,
) -> bool:
    """Build the UI if needed. Returns True when a usable build is in place.

    A stale build that cannot be refreshed (no npm, failed build) is still
    usable: the app starts with the previous interface and a warning, instead
    of not starting at all.
    """

    state = inspect(frontend, static)
    if not force and not state.needs_build:
        return True

    npm = npm or _npm()
    if npm is None:
        if state.built:
            log("Внимание: интерфейс устарел, но npm не найден — запускаю прежнюю сборку. "
                "Установите Node.js, чтобы обновить интерфейс.")
            return True
        log("Интерфейс не собран, а npm не найден. Установите Node.js (вместе с ним ставится npm).")
        return False

    log("Обновляю интерфейс…" if state.built else "Первая сборка интерфейса…")
    if state.needs_install:
        install = "ci" if (frontend / LOCKFILE).is_file() else "install"
        if run([npm, install], frontend) != 0:
            log("Не удалось установить пакеты интерфейса (npm " + install + ").")
            return (static / "index.html").is_file()
    if run([npm, "run", "build"], frontend) != 0:
        log("Сборка интерфейса не удалась.")
        # Vite empties the output folder first, so a failed build may leave nothing.
        return (static / "index.html").is_file()
    write_stamp(frontend, static)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--if-stale", action="store_true", help="rebuild only when frontend sources changed")
    args = parser.parse_args(argv)
    return 0 if ensure_built(force=not args.if_stale) else 1


if __name__ == "__main__":
    sys.exit(main())
