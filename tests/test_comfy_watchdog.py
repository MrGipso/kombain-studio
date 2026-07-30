from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import backend.services.comfy_watchdog as comfy_watchdog_module
from backend.services.comfy_watchdog import ComfyWatchdog, _build_command
from core.config import Settings


def _portable(tmp_path: Path) -> Path:
    root = tmp_path / "ComfyUI-Portable"
    executable = root / "python_embeded" / "python.exe"
    main_py = root / "ComfyUI" / "main.py"
    executable.parent.mkdir(parents=True)
    main_py.parent.mkdir(parents=True)
    executable.write_bytes(b"python")
    main_py.write_text("# comfy", encoding="utf-8")
    return root


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "comfy_watchdog_enabled": True,
        "comfy_install_dir": str(_portable(tmp_path)),
        "comfy_watchdog_failure_threshold": 3,
        "comfy_watchdog_max_restarts": 3,
    }
    values.update(overrides)
    return Settings(**values)


def test_build_command_uses_direct_portable_python_and_explicit_listen(tmp_path):
    settings = _settings(
        tmp_path,
        comfy_watchdog_listen_address="0.0.0.0",
        comfy_watchdog_sage_attention=True,
    )

    executable, arguments, cwd = _build_command(settings)

    assert executable == (cwd / "python_embeded" / "python.exe").resolve()
    assert arguments[:5] == [
        "-I",
        "-W",
        "ignore::FutureWarning",
        "ComfyUI/main.py",
        "--windows-standalone-build",
    ]
    assert "--use-sage-attention" in arguments
    assert arguments[-2:] == ["--listen", "0.0.0.0"]


def test_disabled_watchdog_is_passive(tmp_path):
    settings = _settings(tmp_path, comfy_watchdog_enabled=False)
    watchdog = ComfyWatchdog(
        settings_loader=lambda: settings,
        health_probe=lambda *_args: (_ for _ in ()).throw(
            AssertionError("disabled watchdog must not probe")
        ),
    )

    state = watchdog.check_once()

    assert state["status"] == "disabled"
    assert state["consecutive_failures"] == 0


def test_transient_failures_do_not_restart(tmp_path, monkeypatch):
    settings = _settings(tmp_path, comfy_watchdog_failure_threshold=3)
    watchdog = ComfyWatchdog(
        settings_loader=lambda: settings,
        health_probe=lambda *_args: False,
    )
    restarts = []
    monkeypatch.setattr(
        watchdog,
        "_restart",
        lambda current, now: restarts.append((current, now)),
    )

    first = watchdog.check_once()
    second = watchdog.check_once()

    assert first["status"] == "degraded"
    assert second["status"] == "degraded"
    assert second["consecutive_failures"] == 2
    assert restarts == []


def test_failure_threshold_starts_one_restart(tmp_path, monkeypatch):
    settings = _settings(tmp_path, comfy_watchdog_failure_threshold=2)
    watchdog = ComfyWatchdog(
        settings_loader=lambda: settings,
        health_probe=lambda *_args: False,
    )
    restarts = []

    def fake_restart(current, now):
        restarts.append((current, now))
        watchdog._update(status="restarting")

    monkeypatch.setattr(watchdog, "_restart", fake_restart)

    watchdog.check_once()
    state = watchdog.check_once()

    assert state["status"] == "restarting"
    assert len(restarts) == 1


def test_restart_replaces_matching_orphan_before_launch(tmp_path, monkeypatch):
    settings = _settings(
        tmp_path,
        comfy_watchdog_failure_threshold=2,
        comfy_watchdog_startup_timeout_seconds=60,
    )
    watchdog = ComfyWatchdog(
        settings_loader=lambda: settings,
        health_probe=lambda *_args: False,
        monotonic=lambda: 100.0,
    )
    events = []
    fake_process = SimpleNamespace(pid=4567, poll=lambda: None)

    monkeypatch.setattr(
        watchdog,
        "_discover_managed_pids",
        lambda executable: events.append(("discover", executable)) or [1234],
    )
    monkeypatch.setattr(
        watchdog,
        "_terminate_pid",
        lambda pid: events.append(("terminate", pid)),
    )

    def fake_popen(command, **kwargs):
        events.append(("launch", command, kwargs))
        return fake_process

    monkeypatch.setattr(comfy_watchdog_module.subprocess, "Popen", fake_popen)

    assert watchdog.check_once()["status"] == "degraded"
    state = watchdog.check_once()

    assert [event[0] for event in events] == ["discover", "terminate", "launch"]
    assert events[1] == ("terminate", 1234)
    assert events[2][1][0].endswith("python.exe")
    assert "ComfyUI/main.py" in events[2][1]
    assert state["status"] == "restarting"
    assert state["managed_pid"] == 4567
    assert state["consecutive_failures"] == 0
    assert state["restart_count_window"] == 1
    assert watchdog._startup_deadline == 160.0
    for stream in watchdog._process_streams or ():
        stream.close()


def test_restart_limit_stops_a_crash_loop(tmp_path, monkeypatch):
    settings = _settings(
        tmp_path,
        comfy_watchdog_failure_threshold=2,
        comfy_watchdog_max_restarts=1,
    )
    watchdog = ComfyWatchdog(
        settings_loader=lambda: settings,
        health_probe=lambda *_args: False,
        monotonic=lambda: 100.0,
    )
    watchdog._restart_times.append(99.0)
    monkeypatch.setattr(
        watchdog,
        "_restart",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("restart limit must prevent launch")
        ),
    )

    watchdog.check_once()
    state = watchdog.check_once()

    assert state["status"] == "restart_limit"
    assert state["restart_count_window"] == 1


def test_recovery_resumes_material_checkpoints_once(tmp_path):
    settings = _settings(tmp_path)
    probes = iter([False, True, True])
    resumes = []
    watchdog = ComfyWatchdog(
        settings_loader=lambda: settings,
        health_probe=lambda *_args: next(probes),
        resume_callback=lambda: resumes.append("called") or ["project-a"],
    )

    assert watchdog.check_once()["status"] == "degraded"
    recovered = watchdog.check_once()
    healthy_again = watchdog.check_once()

    assert recovered["status"] == "healthy"
    assert recovered["resumed_projects"] == ["project-a"]
    assert recovered["last_recovery_at"]
    assert healthy_again["status"] == "healthy"
    assert resumes == ["called"]


def test_missing_portable_layout_is_reported_without_probe(tmp_path):
    settings = Settings(
        comfy_watchdog_enabled=True,
        comfy_install_dir=str(tmp_path / "missing"),
    )
    watchdog = ComfyWatchdog(
        settings_loader=lambda: settings,
        health_probe=lambda *_args: (_ for _ in ()).throw(
            AssertionError("misconfigured watchdog must not probe")
        ),
    )

    state = watchdog.check_once()

    assert state["status"] == "misconfigured"
    assert "не найден" in state["last_error"].lower()


def test_multiple_listen_addresses_are_rejected(tmp_path):
    settings = _settings(
        tmp_path,
        comfy_watchdog_listen_address="127.0.0.1,192.168.191.57",
    )
    watchdog = ComfyWatchdog(
        settings_loader=lambda: settings,
        health_probe=lambda *_args: (_ for _ in ()).throw(
            AssertionError("invalid listen address must not probe")
        ),
    )

    state = watchdog.check_once()

    assert state["status"] == "misconfigured"
    assert "один ip-адрес" in state["last_error"].lower()
