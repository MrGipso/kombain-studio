import pytest

import content_pipeline_gui as pipeline
from core import export


class Cancelled(Exception):
    pass


class FakeProcess:
    def __init__(self):
        self.stdout = self
        self.terminated = False
        self.killed = False
        self.closed = False

    def __iter__(self):
        yield "working\n"

    def poll(self):
        return None if not (self.terminated or self.killed) else -15

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return -15 if self.terminated or self.killed else 0

    def close(self):
        self.closed = True


def _cancel(_message):
    raise Cancelled("stop")


def test_pipeline_callback_cancellation_terminates_child(monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr(pipeline.subprocess, "Popen", lambda *a, **k: process)

    with pytest.raises(Cancelled):
        pipeline.run_process(["tool", "arg"], _cancel)

    assert process.terminated is True
    assert process.closed is True


def test_export_callback_cancellation_terminates_child(monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr(export.subprocess, "Popen", lambda *a, **k: process)
    calls = {"count": 0}

    def cancel_after_start(_message):
        calls["count"] += 1
        if calls["count"] > 1:
            raise Cancelled("stop")

    with pytest.raises(Cancelled):
        export.run_ffmpeg(["tool", "arg"], cancel_after_start)

    assert process.terminated is True
    assert process.closed is True
