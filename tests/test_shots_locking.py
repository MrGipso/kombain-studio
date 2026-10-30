import asyncio
from pathlib import Path

from backend.routers import shots as shots_router
from backend.services import jobs
from core.config import Settings
from core.project import create_project


def test_shot_worker_reads_scenes_after_acquiring_project_lock(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "projects"
    settings = Settings(output_base=str(root))
    project = create_project("Shot Lock", root, settings=settings)
    project.write_json("scenes", [{"index": 1, "title": "stale"}])
    monkeypatch.setattr(shots_router, "load_settings", lambda: settings)
    monkeypatch.setenv("PIPELINE_JOBS_DB", str(tmp_path / "jobs.sqlite3"))
    jobs._initialized_db_path = None

    captured: dict[str, object] = {}

    def fake_generate(
        project_dir,
        scenes,
        effective,
        log,
        on_scene_shots=None,
        **_kwargs,
    ):
        captured["project_dir"] = project_dir
        captured["scenes"] = scenes
        captured["settings"] = effective
        return [], []

    monkeypatch.setattr(shots_router, "run_shot_generation", fake_generate)

    async def run_request():
        response = await shots_router.generate_shots(project.dir.name)
        # The route's advisory precheck already ran, but the SSE worker has not
        # started until the response body is consumed.
        project.write_json("scenes", [{"index": 1, "title": "current"}])
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    payload = asyncio.run(run_request())

    assert captured["scenes"] == [{"index": 1, "title": "current"}]
    assert '"event": "done"' in payload
