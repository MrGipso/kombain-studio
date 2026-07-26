from __future__ import annotations

import json
import wave
from pathlib import Path

from core import omnivoice_tts
from core.script_workflow import build_voice_plan


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\x00" * 4800)


def test_render_plan_uses_one_batch_process_for_all_chunks(tmp_path, monkeypatch):
    env_root = tmp_path / "python"
    entrypoint = env_root / "Scripts" / "omnivoice-infer.exe"
    python = env_root / "python.exe"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_bytes(b"")
    python.write_bytes(b"")
    calls = []

    def fake_process(command, log, *, cancel_check=None):
        calls.append(command)
        assert callable(cancel_check)
        request = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        _write_wav(Path(request["reference_audio"]))
        for chunk in request["chunks"]:
            _write_wav(Path(chunk["output"]))
        return 0

    monkeypatch.setattr(omnivoice_tts, "resolve_executable", lambda *_args: str(entrypoint))
    monkeypatch.setattr(omnivoice_tts, "run_streaming_process", fake_process)
    plan = build_voice_plan("Первый фрагмент. Второй фрагмент!")
    profile = {
        "id": "preset",
        "type": "preset",
        "language": "ru",
        "instruct": "male, calm",
    }

    result = omnivoice_tts.render_plan(
        plan,
        profile,
        tmp_path / "project" / "voice",
        cli="omnivoice-infer",
        model="test-model",
        generation={"num_step": 16, "guidance_scale": 2.5},
        cancel_check=lambda: False,
    )

    assert len(calls) == 1
    assert result["manifest"]["generation"]["num_step"] == 16
    assert len(result["manifest"]["chunks"]) == 2
    assert Path(result["path"]).is_file()
