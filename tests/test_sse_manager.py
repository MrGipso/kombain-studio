import inspect

import pytest

from backend.routers import export_router, music, scenes, shots, transcription
from backend.services.sse_manager import SSEManager


def test_targeted_emit_does_not_terminate_another_run_for_the_same_project():
    manager = SSEManager()
    first_id, first_queue = manager.subscribe("project")
    second_id, second_queue = manager.subscribe("project")
    assert first_id != second_id
    first_queue.get_nowait()
    second_queue.get_nowait()

    assert manager.emit_to(first_id, "error", {"message": "duplicate run"}) is True

    assert first_queue.get_nowait() == {
        "event": "error",
        "data": {"message": "duplicate run"},
    }
    assert second_queue.empty()


def test_targeted_emit_is_safe_after_subscriber_disconnects():
    manager = SSEManager()
    sub_id, _queue = manager.subscribe("project")
    manager.unsubscribe(sub_id)

    assert manager.emit_to(sub_id, "done", {}) is False


@pytest.mark.parametrize(
    "route_module",
    [transcription, scenes, shots, music, export_router],
)
def test_long_running_route_events_are_scoped_to_the_starting_stream(route_module):
    source = inspect.getsource(route_module)

    assert "sse.emit(" not in source
