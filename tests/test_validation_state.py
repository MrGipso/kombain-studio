from core.project import Project
from core.validation import write_validation_report


def test_validation_report_does_not_clear_stale_upstream_state(tmp_path):
    project = Project(name="demo", dir=tmp_path)
    project.mark_stage_completed("scenes", [{"index": 1}])
    project.mark_stage_completed("shots", [{"index": 1}])
    project.mark_stage_completed("assets", [{"shot_index": 1}])
    project.mark_stage_completed("scenes", [{"index": 1}, {"index": 2}])

    write_validation_report(project)

    assert project.stage_state("shots")["status"] == "stale"
    assert project.stage_state("validation")["status"] != "completed"


def test_validation_stage_completes_when_upstream_is_current(tmp_path):
    project = Project(name="demo", dir=tmp_path)
    project.mark_stage_completed("transcript", [{"text": "hello"}])
    project.mark_stage_completed("scenes", [{"index": 1}])
    project.mark_stage_completed("shots", [{"index": 1}])
    project.mark_stage_completed("assets", [{"shot_index": 1}])
    project.mark_stage_completed("timeline", {"duration": 1})

    write_validation_report(project)

    assert project.stage_state("validation")["status"] == "completed"


def test_validation_report_cannot_complete_over_stale_timeline(tmp_path):
    project = Project(name="demo", dir=tmp_path)
    project.mark_stage_completed("transcript", [{"text": "hello"}])
    project.mark_stage_completed("scenes", [{"index": 1}])
    project.mark_stage_completed("shots", [{"index": 1}])
    project.mark_stage_completed("assets", [{"shot_index": 1}])
    project.mark_stage_completed("timeline", {"duration": 1})
    project.invalidate_stage("timeline", reason="music changed")

    write_validation_report(project)

    assert project.stage_state("timeline")["status"] == "stale"
    assert project.stage_state("validation")["status"] != "completed"
