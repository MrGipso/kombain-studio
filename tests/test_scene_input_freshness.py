import json

from backend.routers.scenes import _scene_input_payload
from core.project import Project


SEGMENTS = [
    {"start": "00:00:00.000", "end": "00:00:10.000", "text": "alpha one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen"},
    {"start": "00:00:10.000", "end": "00:00:20.000", "text": "beta one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen"},
    {"start": "00:00:20.000", "end": "00:00:30.000", "text": "gamma one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen"},
]


def _write_script(project: Project, beats: list[dict]) -> None:
    (project.dir / "00_script.json").write_text(
        json.dumps({"beats": beats}),
        encoding="utf-8",
    )


def test_scene_input_hash_includes_script_only_when_alignment_uses_it(tmp_path):
    project = Project(name="demo", dir=tmp_path)
    beats = [
        {"title": "A", "text": SEGMENTS[0]["text"]},
        {"title": "B", "text": SEGMENTS[1]["text"]},
        {"title": "C", "text": SEGMENTS[2]["text"]},
    ]
    _write_script(project, beats)

    payload = _scene_input_payload(project, SEGMENTS, {})
    first_hash = project.content_hash(payload)

    assert payload["script"]["artifact"] == "00_script.json"
    beats[1]["title"] = "Changed title"
    _write_script(project, beats)
    changed_payload = _scene_input_payload(project, SEGMENTS, {})
    assert project.content_hash(changed_payload) != first_hash


def test_unusable_script_is_not_a_scene_input(tmp_path):
    project = Project(name="demo", dir=tmp_path)
    _write_script(project, [
        {"title": "A", "text": SEGMENTS[0]["text"]},
        {"title": "B", "text": SEGMENTS[1]["text"]},
    ])

    payload = _scene_input_payload(project, SEGMENTS, {})

    assert "script" not in payload
