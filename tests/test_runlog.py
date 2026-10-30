import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.routers import script as script_router
from backend.routers import sfx as sfx_router
from backend.services import runlog


def _records(project_dir: Path) -> list[dict]:
    records: list[dict] = []
    for path in sorted((project_dir / "logs").glob("runs-*.jsonl")):
        records.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return records


class _FakeJob:
    def __init__(self, job_id: str):
        self.job_id = ""
        self._durable_id = job_id
        self.event = threading.Event()

    def __enter__(self):
        self.job_id = self._durable_id
        return self.event

    def __exit__(self, exc_type, exc, tb):
        return False


def test_append_retains_legacy_fields_and_adds_structured_envelope(tmp_path):
    runlog.append(
        tmp_path,
        "Render",
        "frame 4",
        run_id="run-1",
        job_id="job-1",
        event="progress",
        fields={"frame": 4, "output": tmp_path / "out.mp4"},
    )

    record = _records(tmp_path)[0]
    assert {"ts", "job", "msg"} <= record.keys()
    assert record["job"] == "Render"
    assert record["msg"] == "frame 4"
    assert record["run_id"] == "run-1"
    assert record["job_id"] == "job-1"
    assert record["event"] == "progress"
    assert record["fields"]["frame"] == 4
    assert record["fields"]["output"] == str(tmp_path / "out.mp4")


def test_tee_uses_one_run_id_for_the_whole_legacy_stream(tmp_path):
    downstream: list[str] = []
    callback = runlog.tee(tmp_path, "Transcription", downstream.append)

    callback("first")
    callback("second")

    records = _records(tmp_path)
    assert downstream == ["first", "second"]
    assert {record["run_id"] for record in records} == {callback.run_id}
    assert [record["event"] for record in records] == ["progress", "progress"]


def test_tee_does_not_abort_job_when_progress_sink_cannot_render_unicode(tmp_path):
    def broken_console(_message: str) -> None:
        raise UnicodeEncodeError("cp1251", "→", 0, 1, "unsupported")

    callback = runlog.tee(tmp_path, "Storyboard", broken_console)

    callback("Общий план → деталь")

    assert _records(tmp_path)[0]["msg"] == "Общий план → деталь"


def test_lifecycle_records_start_result_and_error_without_hiding_exception(tmp_path):
    with runlog.run(
        tmp_path,
        "Structure",
        job_id="durable-1",
        fields={"operation": "script.structure"},
    ) as report:
        report.outcome("Structure ready", beats=8)

    with pytest.raises(RuntimeError, match="model offline"):
        with runlog.run(tmp_path, "Structure", job_id="durable-2"):
            raise RuntimeError("model offline")

    records = _records(tmp_path)
    first_run = [record for record in records if record.get("job_id") == "durable-1"]
    failed_run = [record for record in records if record.get("job_id") == "durable-2"]
    assert [record["event"] for record in first_run] == ["start", "result"]
    assert first_run[-1]["msg"] == "Structure ready"
    assert first_run[-1]["fields"]["beats"] == 8
    assert isinstance(first_run[-1]["fields"]["duration_ms"], int)
    assert [record["event"] for record in failed_run] == ["start", "error"]
    assert failed_run[-1]["level"] == "error"
    assert failed_run[-1]["fields"]["error_type"] == "RuntimeError"


def test_rotation_and_retention_bound_total_files(tmp_path, monkeypatch):
    monkeypatch.setenv(runlog.RUNLOG_MAX_FILES_ENV, "2")
    monkeypatch.setenv(runlog.RUNLOG_MAX_BYTES_ENV, "4096")
    message = "x" * 3500

    for index in range(3):
        runlog.append(tmp_path, "Noisy encoder", f"{index}:{message}", run_id="one-run")

    files = sorted((tmp_path / "logs").glob("runs-*.jsonl"))
    assert len(files) == 2
    assert all(path.stat().st_size <= 4096 for path in files)
    assert len(_records(tmp_path)) == 2


def test_logging_io_failure_never_breaks_job(tmp_path):
    not_a_project = tmp_path / "project"
    not_a_project.write_text("this path is a file", encoding="utf-8")

    runlog.append(not_a_project, "Render", "start")
    with runlog.run(not_a_project, "Render") as report:
        report.outcome("done")


def test_recent_records_are_bounded_newest_first_and_ignore_unmanaged_files(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    base_records = [
        {"ts": "2026-07-10T10:00:00Z", "job": "Render", "msg": "old"},
        {"ts": "2026-07-10T10:01:00Z", "job": "Render", "msg": "middle"},
    ]
    (logs_dir / "runs-20260710.jsonl").write_text(
        "\n".join(json.dumps(item) for item in base_records) + "\nnot-json\n",
        encoding="utf-8",
    )
    (logs_dir / "runs-20260710-001.jsonl").write_text(
        json.dumps({
            "v": 1,
            "ts": "2026-07-10T10:02:00Z",
            "job": "Export",
            "msg": "newest",
            "run_id": "run-2",
            "event": "result",
            "level": "info",
        }) + "\n",
        encoding="utf-8",
    )
    (logs_dir / "private.jsonl").write_text(
        json.dumps({"ts": "later", "job": "Leak", "msg": "must not be read"}),
        encoding="utf-8",
    )

    records = runlog.recent_records(tmp_path, limit=2)

    assert [record["msg"] for record in records] == ["newest", "middle"]
    assert records[0]["run_id"] == "run-2"
    assert records[1]["event"] == "log"  # legacy line was normalised
    assert set(records[0]) == {
        "v", "ts", "job", "msg", "run_id", "job_id", "event", "level", "fields",
    }


def test_recent_records_enforces_hard_result_limit(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "runs-20260710.jsonl").write_text(
        "\n".join(
            json.dumps({"ts": str(index), "job": "Task", "msg": str(index)})
            for index in range(runlog.MAX_READ_RECORDS + 10)
        ),
        encoding="utf-8",
    )

    records = runlog.recent_records(tmp_path, limit=10_000)

    assert len(records) == runlog.MAX_READ_RECORDS
    assert records[0]["msg"] == str(runlog.MAX_READ_RECORDS + 9)


def test_script_structure_run_is_correlated_with_durable_job(monkeypatch, tmp_path):
    project = SimpleNamespace(
        dir=tmp_path / "script-project",
        mark_stage_completed=lambda *_args, **_kwargs: None,
    )
    project.dir.mkdir()
    result = {"hooks": ["hook"], "beats": [{"title": "beat"}]}
    monkeypatch.setattr(script_router, "_resolve_project", lambda _project_id: project)
    monkeypatch.setattr(script_router, "_structure_sync", lambda _pdir, _script: result)
    monkeypatch.setattr(
        script_router.jobs,
        "project_job",
        lambda *_args, **_kwargs: _FakeJob("script-job-123"),
    )

    response = asyncio.run(script_router.generate_structure("script-project"))

    assert response == result
    records = _records(project.dir)
    assert [record["event"] for record in records] == ["start", "result"]
    assert {record["job_id"] for record in records} == {"script-job-123"}
    assert records[-1]["fields"]["operation"] == "script.structure"
    assert records[-1]["fields"]["beats"] == 1


def test_sfx_failure_is_written_to_correlated_run(monkeypatch, tmp_path):
    project_dir = tmp_path / "sfx-project"
    project_dir.mkdir()
    card = SimpleNamespace(
        shot_index=1,
        scene_title="Rain",
        start=0,
        end=5,
        description="Rain on a window",
        status="pending",
        sfx_query="gentle rain",
        sfx_output="",
    )
    monkeypatch.setattr(sfx_router, "_resolve_project", lambda _project_id: project_dir)
    monkeypatch.setattr(
        sfx_router,
        "load_settings",
        lambda: SimpleNamespace(woosh_url="http://127.0.0.1:9999"),
    )
    monkeypatch.setattr(sfx_router.board_store, "load_asset_board", lambda _pdir: [card])
    monkeypatch.setattr(sfx_router.sfx_lib, "woosh_ping", lambda *_args: False)
    monkeypatch.setattr(
        sfx_router.jobs,
        "project_job",
        lambda *_args, **_kwargs: _FakeJob("sfx-job-123"),
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(sfx_router.generate("sfx-project", sfx_router.GenerateRequest(card_indices=[0])))

    assert caught.value.status_code == 502
    records = _records(project_dir)
    assert [record["event"] for record in records] == ["start", "error"]
    assert {record["job_id"] for record in records} == {"sfx-job-123"}
    assert records[-1]["fields"]["operation"] == "sfx.generate"
    assert records[-1]["fields"]["error_type"] == "RuntimeError"
