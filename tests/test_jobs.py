import importlib
import sqlite3
import threading
import time

import pytest

from backend.services import jobs as jobs_module


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    database = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("PIPELINE_JOBS_DB", str(database))
    module = importlib.reload(jobs_module)
    yield module
    # Every test finishes its contexts, so reloading safely clears in-memory
    # locks and prevents the environment-specific DB path leaking to a later
    # test module in the same pytest process.
    importlib.reload(module)


def test_successful_job_persists_progress_and_log_message(jobs, tmp_path):
    database = tmp_path / "jobs.sqlite3"
    assert not database.exists(), "database initialization must stay lazy"

    messages = []
    with jobs.project_job("project-a", "Render", resource="GPU") as event:
        assert jobs.running_job("project-a") == "Render"
        jobs.update_progress(event, 0.5, "halfway")
        jobs.checking_logger(messages.append, event)("frame 5/10")

    assert messages == ["frame 5/10"]
    assert jobs.running_job("project-a") == ""
    record = jobs.job_history("project-a")[0]
    assert record["status"] == "succeeded"
    assert record["resource"] == "gpu"
    assert record["progress"] == 1.0
    assert record["message"] == "frame 5/10"
    assert record["error"] == ""
    assert record["created_at"]
    assert record["started_at"]
    assert record["updated_at"]
    assert record["finished_at"]
    assert jobs.job_record(record["id"]) == record


def test_project_and_global_resource_locks_fail_fast(jobs):
    with jobs.project_job("project-a", "GPU task", resource="gpu"):
        with pytest.raises(jobs.ProjectBusy, match="Проект занят"):
            with jobs.project_job("project-a", "Second project task"):
                pass

        with pytest.raises(jobs.ProjectBusy, match="Ресурс.*gpu.*занят"):
            with jobs.project_job("project-b", "Second GPU task", resource="GPU"):
                pass

        # An unrelated resource remains available to another project.
        with jobs.project_job("project-b", "Encode", resource="ffmpeg"):
            assert jobs.running_job("project-b") == "Encode"

    assert {row["name"] for row in jobs.job_history()} == {"GPU task", "Encode"}


@pytest.mark.parametrize("active_resource", ["gpu", "llm", "comfy", "stock"])
@pytest.mark.parametrize("waiting_resource", ["gpu", "llm", "comfy", "stock"])
def test_gpu_backed_resource_subtypes_share_one_lease(
    jobs,
    active_resource,
    waiting_resource,
):
    with jobs.project_job(
        "project-a",
        f"Active {active_resource}",
        resource=active_resource,
    ):
        with pytest.raises(jobs.ProjectBusy) as caught:
            with jobs.project_job(
                "project-b",
                f"Waiting {waiting_resource}",
                resource=waiting_resource,
            ):
                pass

        message = str(caught.value)
        assert "Ресурс «gpu»" in message
        assert "GPU/VRAM" in message
        assert f"Active {active_resource}" in message

        # GPU-heavy work must not unnecessarily block an unrelated encoder.
        with jobs.project_job("project-b", "Encode", resource="ffmpeg"):
            pass

    records = {row["name"]: row for row in jobs.job_history()}
    assert records[f"Active {active_resource}"]["resource"] == active_resource
    assert records["Encode"]["resource"] == "ffmpeg"
    assert f"Waiting {waiting_resource}" not in records


def test_gpu_lease_is_released_after_failure(jobs):
    with pytest.raises(RuntimeError, match="inference failed"):
        with jobs.project_job("project-a", "Vision QA", resource="llm"):
            raise RuntimeError("inference failed")

    with jobs.project_job("project-b", "Render after failure", resource="comfy"):
        pass

    records = {row["name"]: row for row in jobs.job_history()}
    assert records["Vision QA"]["status"] == "failed"
    assert records["Vision QA"]["resource"] == "llm"
    assert records["Render after failure"]["status"] == "succeeded"
    assert records["Render after failure"]["resource"] == "comfy"


def test_gpu_lease_is_released_after_cancellation(jobs):
    with pytest.raises(jobs.JobCancelled, match="отменена пользователем"):
        with jobs.project_job("project-a", "Stock vision", resource="stock") as event:
            assert jobs.request_cancel("project-a") == "Stock vision"
            jobs.check(event)

    with jobs.project_job("project-b", "GPU retry", resource="gpu"):
        pass

    records = {row["name"]: row for row in jobs.job_history()}
    assert records["Stock vision"]["status"] == "cancelled"
    assert records["Stock vision"]["resource"] == "stock"
    assert records["GPU retry"]["status"] == "succeeded"


def test_short_mutation_uses_project_lock_without_polluting_job_history(jobs):
    with jobs.project_mutation("project-a", "Save Asset Board"):
        assert jobs.running_job("project-a") == "Save Asset Board"
        with pytest.raises(jobs.ProjectBusy, match="Save Asset Board"):
            with jobs.project_job("project-a", "Render"):
                pass

    assert jobs.running_job("project-a") == ""
    assert jobs.job_history("project-a") == []

    # The inverse ordering is protected by the same lock as well.
    with jobs.project_job("project-a", "Render"):
        with pytest.raises(jobs.ProjectBusy, match="Render"):
            with jobs.project_mutation("project-a", "Save Asset Board"):
                pass


def test_bulk_mutation_releases_every_partial_lock_on_busy_project(jobs):
    # project-a sorts before project-b, so the bulk attempt acquires A before
    # discovering that B is busy.  A must be available immediately afterwards.
    with jobs.project_job("project-b", "Render"):
        with pytest.raises(jobs.ProjectBusy, match="Render"):
            with jobs.bulk_project_mutation(
                ["project-b", "project-a"],
                "Global settings update",
            ):
                pass

        assert jobs.running_job("project-a") == ""
        with jobs.project_mutation("project-a", "Independent edit"):
            assert jobs.running_job("project-a") == "Independent edit"


def test_settings_mutation_is_separately_serialized(jobs):
    with jobs.settings_mutation():
        with pytest.raises(jobs.ProjectBusy, match="Настройки заняты"):
            with jobs.settings_mutation():
                pass


def test_cancellation_is_recorded_and_lock_is_released(jobs):
    with pytest.raises(jobs.JobCancelled, match="отменена пользователем"):
        with jobs.project_job("project-a", "Transcription") as event:
            assert jobs.request_cancel("project-a") == "Transcription"
            jobs.check(event)

    record = jobs.job_history("project-a")[0]
    assert record["status"] == "cancelled"
    assert record["message"] == "Cancellation requested"
    assert "отменена пользователем" in record["error"]

    with jobs.project_job("project-a", "Retry"):
        pass
    assert jobs.job_history("project-a")[0]["status"] == "succeeded"


def test_failure_is_recorded_without_masking_original_error(jobs):
    with pytest.raises(RuntimeError, match="encoder failed"):
        with jobs.project_job("project-a", "Export"):
            raise RuntimeError("encoder failed")

    record = jobs.job_history("project-a")[0]
    assert record["status"] == "failed"
    assert record["message"] == "Failed"
    assert record["error"] == "RuntimeError: encoder failed"


def test_running_row_is_recovered_as_interrupted_after_restart(jobs, tmp_path):
    with jobs.project_job("project-a", "Old render"):
        pass

    database = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE jobs
               SET status = 'running', message = '', error = '', finished_at = NULL
            """
        )
        connection.commit()

    restarted = importlib.reload(jobs)
    record = restarted.job_history("project-a")[0]
    assert record["status"] == "interrupted"
    assert record["message"] == "Interrupted by server restart"
    assert record["error"] == "Server stopped before the job completed"
    assert record["finished_at"]


def test_update_progress_validates_fraction(jobs):
    with jobs.project_job("project-a", "Render") as event:
        with pytest.raises(ValueError, match="between 0 and 1"):
            jobs.update_progress(event, 1.1)

    # Unknown/finished events are intentionally harmless for late callbacks.
    jobs.update_progress(event, 0.5, "late")


def test_background_project_job_is_durable_before_return_and_finishes(jobs):
    release = threading.Event()
    completed = threading.Event()

    def worker(cancel_event):
        jobs.update_progress(cancel_event, 0.5, "background checkpoint")
        release.wait(timeout=2)
        completed.set()

    job_id = jobs.start_background_project_job(
        "project-a",
        "Unattended materials",
        worker,
        resource="comfy",
    )
    record = jobs.job_record(job_id)
    assert record is not None
    assert record["status"] == "running"
    deadline = time.monotonic() + 1
    while record["message"] != "background checkpoint" and time.monotonic() < deadline:
        time.sleep(0.01)
        record = jobs.job_record(job_id)
        assert record is not None
    assert record["message"] == "background checkpoint"
    assert jobs.running_job("project-a") == "Unattended materials"

    release.set()
    assert completed.wait(timeout=2)
    for _ in range(100):
        record = jobs.job_record(job_id)
        if record and record["status"] == "succeeded":
            break
        threading.Event().wait(0.01)
    assert record["status"] == "succeeded"


def test_background_project_job_reports_fast_worker_failure_in_durable_record(jobs):
    def worker(_cancel_event):
        raise RuntimeError("provider failed immediately")

    job_id = jobs.start_background_project_job(
        "project-a",
        "Unattended materials",
        worker,
        resource="comfy",
    )
    deadline = time.monotonic() + 1
    record = jobs.job_record(job_id)
    while record and record["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
        record = jobs.job_record(job_id)

    assert record is not None
    assert record["status"] == "failed"
    assert "provider failed immediately" in record["error"]
