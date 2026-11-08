import importlib
import urllib.error
from io import BytesIO

import pytest

from backend.services import gpu_handoff
from backend.services import jobs as jobs_module
from core import lm
from core.config import Settings

GB = 1 << 30


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    module = importlib.reload(jobs_module)
    yield module
    importlib.reload(module)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _release(settings, readings, *, free_ok=True):
    """Run release_comfy_for_llm against scripted VRAM readings."""
    readings = list(readings)
    freed = []
    clock = FakeClock()

    def vram(url):
        return readings.pop(0) if len(readings) > 1 else readings[0]

    def free(url):
        freed.append(url)
        return free_ok

    status = gpu_handoff.release_comfy_for_llm(
        settings, vram_reserved=vram, free_memory=free, sleep=clock.sleep, clock=clock,
    )
    return status, freed


LOCAL = Settings(lm_url="http://127.0.0.1:1234/v1", comfy_url="http://127.0.0.1:8188", lm_provider="lmstudio")


@pytest.mark.parametrize(
    ("lm_url", "comfy_url", "expected"),
    [
        ("http://127.0.0.1:1234/v1", "http://localhost:8188", True),
        ("http://[::1]:1234/v1", "http://127.0.0.1:8188", True),
        ("http://192.168.0.5:1234/v1", "http://192.168.0.5:8188", True),
        ("http://192.168.191.57:1234/v1", "http://127.0.0.1:8188", False),
        ("", "http://127.0.0.1:8188", False),
    ],
)
def test_same_machine(lm_url, comfy_url, expected):
    assert gpu_handoff.same_machine(lm_url, comfy_url) is expected


def test_loaded_comfy_models_are_unloaded_and_awaited():
    status, freed = _release(LOCAL, [24 * GB, 24 * GB, 20 * GB, GB // 2])
    assert status == "freed"
    assert freed == ["http://127.0.0.1:8188"]


def test_idle_comfy_is_left_alone():
    status, freed = _release(LOCAL, [GB // 2])
    assert (status, freed) == ("idle", [])


def test_offline_comfy_is_skipped():
    status, freed = _release(LOCAL, [None])
    assert (status, freed) == ("skipped-offline", [])


def test_cloud_llm_never_touches_comfy():
    cloud = Settings(lm_url="https://api.openai.com/v1", lm_provider="openai_compatible")
    status, freed = _release(cloud, [24 * GB])
    assert (status, freed) == ("skipped-cloud", [])


def test_lm_studio_on_another_machine_never_touches_comfy():
    remote = Settings(lm_url="http://192.168.191.57:1234/v1", comfy_url="http://127.0.0.1:8188", lm_provider="lmstudio")
    status, freed = _release(remote, [24 * GB])
    assert (status, freed) == ("skipped-remote", [])


def test_wait_is_bounded_when_comfy_keeps_its_memory():
    status, freed = _release(LOCAL, [24 * GB])
    assert status == "timeout"
    assert len(freed) == 1


def test_rejected_free_request_is_reported():
    status, _ = _release(LOCAL, [24 * GB], free_ok=False)
    assert status == "failed"


# --- job lease hook ---------------------------------------------------------------

def test_hook_runs_for_llm_jobs_only_and_can_be_removed(jobs):
    calls = []
    remove = jobs.add_acquire_hook("llm", lambda: calls.append("llm"))

    with jobs.project_job("alpha", "Анализ сцен", resource="llm"):
        assert calls == ["llm"]
    with jobs.project_job("alpha", "Генерация", resource="comfy"):
        pass
    with jobs.project_job("alpha", "Без ресурса"):
        pass
    assert calls == ["llm"]

    remove()
    with jobs.project_job("alpha", "Анализ сцен", resource="LLM"):
        pass
    assert calls == ["llm"]


def test_failing_hook_never_fails_the_job(jobs):
    def broken():
        raise RuntimeError("ComfyUI exploded")

    jobs.add_acquire_hook("llm", broken)
    with jobs.project_job("alpha", "Анализ сцен", resource="llm") as cancel_event:
        assert not cancel_event.is_set()
    # The lease was released normally.
    with jobs.project_job("beta", "Генерация", resource="comfy"):
        pass


def test_install_registers_the_handoff_for_llm_jobs(jobs, monkeypatch):
    calls = []
    monkeypatch.setattr(gpu_handoff, "release_comfy_for_llm", lambda: calls.append(1) or "idle")
    remove = gpu_handoff.install()
    try:
        with jobs.project_job("alpha", "План кадров", resource="llm"):
            pass
    finally:
        remove()
    assert calls == [1]


# --- error text ---------------------------------------------------------------------

def test_model_load_failure_is_explained_as_vram_shortage():
    body = b'{"error": {"type": "model_load_failed", "message": "Failed to load LLM \'gemma\': Error: Failed to load model."}}'
    exc = urllib.error.HTTPError("http://127.0.0.1:1234/api/v1/models/load", 500, "Internal Server Error", {}, BytesIO(body))
    text = lm.explain_error(exc)
    assert text.startswith("LM Studio не смог загрузить модель")
    assert "видеокарты" in text
    assert "model_load_failed" in text  # the original server answer is kept
