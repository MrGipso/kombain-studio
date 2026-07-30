"""Bounded ComfyUI process supervision for unattended production.

The watchdog is deliberately conservative:

* transient probe failures never restart ComfyUI immediately;
* only portable-python processes matching the configured executable and
  ``ComfyUI\\main.py`` command line are eligible for termination;
* restart frequency is capped inside a sliding window;
* material checkpoints are resumed only after the API is healthy again.

It does not modify the ComfyUI installation.  The child is launched directly
without a shell, using the existing portable Python and main.py.
"""

from __future__ import annotations

import json
import ipaddress
import logging
import os
import subprocess
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.config import ROOT, Settings, load_settings


logger = logging.getLogger(__name__)

SettingsLoader = Callable[[], Settings]
HealthProbe = Callable[[str, float], bool]
ResumeCallback = Callable[[], list[str]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _probe_comfy(url: str, timeout: float) -> bool:
    base = (url or "http://127.0.0.1:8188").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/system_stats", timeout=timeout) as response:
            return int(getattr(response, "status", 200)) == 200
    except Exception:
        return False


def _bounded_number(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _portable_paths(settings: Settings) -> tuple[Path, Path, Path]:
    raw_root = str(settings.comfy_install_dir or "").strip()
    if not raw_root:
        raise ValueError("Не указан каталог portable ComfyUI")
    root = Path(raw_root).expanduser().resolve()
    executable = root / "python_embeded" / "python.exe"
    main_py = root / "ComfyUI" / "main.py"
    if not root.is_dir():
        raise ValueError("Каталог portable ComfyUI не найден")
    if not executable.is_file():
        raise ValueError("Не найден python_embeded/python.exe")
    if not main_py.is_file():
        raise ValueError("Не найден ComfyUI/main.py")
    return root, executable.resolve(), main_py.resolve()


def _listen_address(settings: Settings) -> str:
    listen = str(settings.comfy_watchdog_listen_address or "127.0.0.1").strip()
    try:
        ipaddress.ip_address(listen)
    except ValueError as exc:
        raise ValueError("--listen должен содержать один IP-адрес") from exc
    return listen


def _build_command(settings: Settings) -> tuple[Path, list[str], Path]:
    root, executable, _main_py = _portable_paths(settings)
    arguments = [
        "-I",
        "-W",
        "ignore::FutureWarning",
        "ComfyUI/main.py",
        "--windows-standalone-build",
    ]
    if bool(settings.comfy_watchdog_sage_attention):
        arguments.append("--use-sage-attention")
    listen = _listen_address(settings)
    if listen and listen != "127.0.0.1":
        arguments.extend(["--listen", listen])
    return executable, arguments, root


class ComfyWatchdog:
    def __init__(
        self,
        *,
        settings_loader: SettingsLoader = load_settings,
        health_probe: HealthProbe = _probe_comfy,
        resume_callback: ResumeCallback | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._settings_loader = settings_loader
        self._health_probe = health_probe
        self._resume_callback = resume_callback
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._process_streams: tuple[Any, Any] | None = None
        self._restart_times: deque[float] = deque()
        self._startup_deadline = 0.0
        self._consecutive_failures = 0
        self._status: dict[str, Any] = {
            "schema_version": 1,
            "enabled": False,
            "status": "stopped",
            "healthy": False,
            "comfy_url": "",
            "listen_address": "",
            "consecutive_failures": 0,
            "restart_count_window": 0,
            "max_restarts": 0,
            "managed_pid": None,
            "last_check_at": None,
            "last_healthy_at": None,
            "last_restart_at": None,
            "last_recovery_at": None,
            "last_error": "",
            "message": "Watchdog ещё не запущен",
            "resumed_projects": [],
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._status, ensure_ascii=False))

    def _update(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="comfy-watchdog",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        self._close_finished_process_streams()
        self._update(status="stopped", message="Watchdog остановлен")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            seconds = 5.0
            try:
                self.check_once()
                settings = self._settings_loader()
                seconds = _bounded_number(
                    settings.comfy_watchdog_poll_seconds,
                    2.0,
                    60.0,
                    5.0,
                )
            except Exception as exc:
                logger.exception("ComfyUI watchdog tick failed")
                self._update(
                    status="error",
                    healthy=False,
                    last_check_at=_utc_now(),
                    last_error=str(exc),
                    message="Внутренняя ошибка watchdog",
                )
            self._stop_event.wait(seconds)

    def _trim_restart_window(self, now: float, settings: Settings) -> None:
        window = _bounded_number(
            settings.comfy_watchdog_restart_window_seconds,
            60.0,
            86400.0,
            1800.0,
        )
        while self._restart_times and now - self._restart_times[0] > window:
            self._restart_times.popleft()

    def check_once(self) -> dict[str, Any]:
        settings = self._settings_loader()
        enabled = bool(settings.comfy_watchdog_enabled)
        now = self._monotonic()
        self._trim_restart_window(now, settings)
        max_restarts = int(
            _bounded_number(settings.comfy_watchdog_max_restarts, 1, 10, 3)
        )
        self._update(
            enabled=enabled,
            comfy_url=str(settings.comfy_url or "http://127.0.0.1:8188"),
            listen_address=str(settings.comfy_watchdog_listen_address or "127.0.0.1"),
            max_restarts=max_restarts,
            restart_count_window=len(self._restart_times),
            last_check_at=_utc_now(),
        )
        if not enabled:
            self._consecutive_failures = 0
            self._update(
                status="disabled",
                healthy=False,
                consecutive_failures=0,
                message="Автоперезапуск ComfyUI выключен",
                last_error="",
            )
            return self.status()

        try:
            _portable_paths(settings)
            _listen_address(settings)
        except ValueError as exc:
            self._update(
                status="misconfigured",
                healthy=False,
                message="Watchdog не настроен",
                last_error=str(exc),
            )
            return self.status()

        healthy = self._health_probe(
            str(settings.comfy_url or "http://127.0.0.1:8188"),
            3.0,
        )
        if healthy:
            previous = str(self.status().get("status", ""))
            recovered = previous in {
                "degraded",
                "restarting",
                "restart_limit",
                "error",
            }
            self._consecutive_failures = 0
            self._startup_deadline = 0.0
            self._close_finished_process_streams()
            self._update(
                status="healthy",
                healthy=True,
                consecutive_failures=0,
                last_healthy_at=_utc_now(),
                last_error="",
                message="ComfyUI отвечает",
                managed_pid=(
                    self._process.pid
                    if self._process is not None and self._process.poll() is None
                    else None
                ),
            )
            if recovered:
                resumed: list[str] = []
                if self._resume_callback is not None:
                    try:
                        resumed = list(self._resume_callback())
                    except Exception as exc:
                        logger.exception("Failed to resume material runs after ComfyUI recovery")
                        self._update(last_error=f"Resume failed: {exc}")
                self._update(
                    last_recovery_at=_utc_now(),
                    resumed_projects=resumed,
                    message=(
                        "ComfyUI восстановлен; очереди продолжены"
                        if resumed
                        else "ComfyUI восстановлен"
                    ),
                )
            return self.status()

        self._close_finished_process_streams()
        if self._startup_deadline > now:
            self._update(
                status="restarting",
                healthy=False,
                message="Ожидание запуска ComfyUI",
                managed_pid=(
                    self._process.pid
                    if self._process is not None and self._process.poll() is None
                    else None
                ),
            )
            # A process that exited before opening the API cannot recover by
            # waiting for the remainder of the startup timeout.
            if self._process is not None and self._process.poll() is not None:
                self._startup_deadline = 0.0
                self._update(last_error=f"ComfyUI exited with code {self._process.returncode}")
            else:
                return self.status()

        self._consecutive_failures += 1
        threshold = int(
            _bounded_number(settings.comfy_watchdog_failure_threshold, 2, 12, 3)
        )
        self._update(
            status="degraded",
            healthy=False,
            consecutive_failures=self._consecutive_failures,
            message=(
                f"ComfyUI не отвечает: проверка "
                f"{self._consecutive_failures}/{threshold}"
            ),
        )
        if self._consecutive_failures < threshold:
            return self.status()
        if len(self._restart_times) >= max_restarts:
            self._update(
                status="restart_limit",
                message="Лимит автоперезапусков ComfyUI исчерпан",
                last_error=(
                    f"Не более {max_restarts} перезапусков за окно наблюдения"
                ),
            )
            return self.status()

        self._restart(settings, now)
        return self.status()

    def _restart(self, settings: Settings, now: float) -> None:
        executable, arguments, root = _build_command(settings)
        self._update(
            status="restarting",
            healthy=False,
            message="Перезапуск ComfyUI",
        )
        pids = self._discover_managed_pids(executable)
        for pid in pids:
            self._terminate_pid(pid)

        log_dir = ROOT / "logs" / "comfy-watchdog"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout = (log_dir / "comfy.stdout.log").open(
            "a",
            encoding="utf-8",
            errors="replace",
        )
        stderr = (log_dir / "comfy.stderr.log").open(
            "a",
            encoding="utf-8",
            errors="replace",
        )
        creationflags = (
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.name == "nt"
            else 0
        )
        try:
            process = subprocess.Popen(
                [str(executable), *arguments],
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                creationflags=creationflags,
            )
        except Exception:
            stdout.close()
            stderr.close()
            raise
        self._process = process
        self._process_streams = (stdout, stderr)
        self._restart_times.append(now)
        self._consecutive_failures = 0
        startup_timeout = _bounded_number(
            settings.comfy_watchdog_startup_timeout_seconds,
            30.0,
            900.0,
            180.0,
        )
        self._startup_deadline = now + startup_timeout
        self._update(
            restart_count_window=len(self._restart_times),
            managed_pid=process.pid,
            last_restart_at=_utc_now(),
            last_error="",
            message=f"ComfyUI запущен, PID {process.pid}; ожидание API",
        )

    def _close_finished_process_streams(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        streams = self._process_streams
        self._process_streams = None
        if streams:
            for stream in streams:
                try:
                    stream.close()
                except Exception:
                    pass

    def _discover_managed_pids(self, executable: Path) -> list[int]:
        if os.name != "nt":
            process = self._process
            return [process.pid] if process is not None and process.poll() is None else []
        environment = os.environ.copy()
        environment["PIPELINE_COMFY_EXPECTED_EXE"] = str(executable)
        script = (
            "$expected=[IO.Path]::GetFullPath($env:PIPELINE_COMFY_EXPECTED_EXE);"
            "Get-CimInstance Win32_Process | "
            "Where-Object {$_.ExecutablePath -and "
            "([IO.Path]::GetFullPath($_.ExecutablePath) -ieq $expected) -and "
            "$_.CommandLine -like '*ComfyUI*main.py*'} | "
            "Select-Object -ExpandProperty ProcessId"
        )
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
                env=environment,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
        except (OSError, subprocess.SubprocessError):
            return []
        pids: list[int] = []
        for line in result.stdout.splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid > 0 and pid != os.getpid():
                pids.append(pid)
        return sorted(set(pids))

    @staticmethod
    def _terminate_pid(pid: int) -> None:
        if pid <= 0 or pid == os.getpid():
            raise ValueError("Refusing to terminate an invalid watchdog PID")
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
            if result.returncode not in {0, 128}:
                raise RuntimeError(
                    f"Не удалось остановить зависший ComfyUI PID {pid}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            return
        os.kill(pid, 15)


_global_watchdog: ComfyWatchdog | None = None
_global_lock = threading.Lock()


def start_watchdog(
    *,
    resume_callback: ResumeCallback | None = None,
) -> ComfyWatchdog:
    global _global_watchdog
    with _global_lock:
        if _global_watchdog is None:
            _global_watchdog = ComfyWatchdog(resume_callback=resume_callback)
        elif resume_callback is not None:
            _global_watchdog._resume_callback = resume_callback
        _global_watchdog.start()
        return _global_watchdog


def stop_watchdog() -> None:
    global _global_watchdog
    with _global_lock:
        watchdog = _global_watchdog
        _global_watchdog = None
    if watchdog is not None:
        watchdog.stop()


def watchdog_status() -> dict[str, Any]:
    with _global_lock:
        watchdog = _global_watchdog
    if watchdog is None:
        settings = load_settings()
        return {
            "schema_version": 1,
            "enabled": bool(settings.comfy_watchdog_enabled),
            "status": "stopped",
            "healthy": False,
            "comfy_url": str(settings.comfy_url or ""),
            "listen_address": str(settings.comfy_watchdog_listen_address or ""),
            "consecutive_failures": 0,
            "restart_count_window": 0,
            "max_restarts": int(settings.comfy_watchdog_max_restarts),
            "managed_pid": None,
            "last_check_at": None,
            "last_healthy_at": None,
            "last_restart_at": None,
            "last_recovery_at": None,
            "last_error": "",
            "message": "Watchdog не запущен",
            "resumed_projects": [],
        }
    return watchdog.status()


__all__ = [
    "ComfyWatchdog",
    "start_watchdog",
    "stop_watchdog",
    "watchdog_status",
]
