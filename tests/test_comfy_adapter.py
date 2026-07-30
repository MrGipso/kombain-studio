import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

import core.comfy as comfy


class _Response:
    def __init__(self, body: bytes):
        self.body = body
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


def test_base_url_normalizes_and_uses_local_default():
    assert comfy.comfy_base_url("  http://localhost:8188/// ") == "http://localhost:8188"
    assert comfy.comfy_base_url("  ") == comfy.DEFAULT_COMFY_URL


def test_queue_prompt_preserves_payload_and_timeout(monkeypatch):
    seen = {}

    def fake_request(url, payload=None, timeout=30):
        seen.update(url=url, payload=payload, timeout=timeout)
        return {"prompt_id": "prompt-7"}

    monkeypatch.setattr(comfy, "comfy_request_json", fake_request)
    prompt_id = comfy.comfy_queue_prompt("http://comfy/", {"12": {"inputs": {}}})

    assert prompt_id == "prompt-7"
    assert seen["url"] == "http://comfy/prompt"
    assert seen["timeout"] == 60
    assert seen["payload"]["prompt"] == {"12": {"inputs": {}}}
    assert seen["payload"]["client_id"]


def test_queue_prompt_rejects_missing_prompt_id(monkeypatch):
    monkeypatch.setattr(comfy, "comfy_request_json", lambda *args, **kwargs: {"number": 3})
    with pytest.raises(RuntimeError, match="did not return prompt_id"):
        comfy.comfy_queue_prompt("http://comfy", {})


def test_wait_retries_url_errors_then_returns_outputs(monkeypatch):
    calls = []

    def fake_request(url, payload=None, timeout=30):
        calls.append((url, timeout))
        if len(calls) == 1:
            raise urllib.error.URLError("warming up")
        return {"id with space": {"status": {"status_str": "success"}, "outputs": {"9": {"images": [{}]}}}}

    monkeypatch.setattr(comfy, "comfy_request_json", fake_request)
    monkeypatch.setattr(comfy.time, "sleep", lambda _: None)

    result = comfy.comfy_wait_for_history("http://comfy/", "id with space", poll_interval=0)

    assert result["outputs"]
    assert calls == [
        ("http://comfy/history/id%20with%20space", 20),
        ("http://comfy/history/id%20with%20space", 20),
    ]


def test_wait_surfaces_comfy_error_status_without_retry(monkeypatch):
    monkeypatch.setattr(
        comfy,
        "comfy_request_json",
        lambda *args, **kwargs: {"p": {"status": {"status_str": "error", "messages": ["bad node"]}}},
    )
    monkeypatch.setattr(comfy.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="bad node"):
        comfy.comfy_wait_for_history("http://comfy", "p")


def test_wait_timeout_keeps_last_transport_error(monkeypatch):
    clock = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(comfy.time, "time", lambda: next(clock))
    monkeypatch.setattr(comfy.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        comfy,
        "comfy_request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )

    with pytest.raises(TimeoutError, match="offline"):
        comfy.comfy_wait_for_history("http://comfy", "p", timeout_seconds=1)


def test_wait_supports_cooperative_cancellation(monkeypatch):
    cancelled = []
    monkeypatch.setattr(
        comfy,
        "comfy_cancel_prompt",
        lambda base_url, prompt_id: cancelled.append((base_url, prompt_id)),
    )

    with pytest.raises(comfy.ComfyCancelledError, match="prompt-9"):
        comfy.comfy_wait_for_history("http://comfy", "prompt-9", cancel_check=lambda: True)
    assert cancelled == [("http://comfy", "prompt-9")]


def test_collect_outputs_flattens_selected_kinds_in_node_order():
    history = {
        "outputs": {
            "1": {"images": [{"filename": "a.png"}], "audio": [{"filename": "skip.wav"}]},
            "2": {"images": [{"filename": "b.png"}], "videos": [{"filename": "c.mp4"}]},
        }
    }
    assert comfy.comfy_collect_outputs(history, ("images", "videos")) == [
        {"filename": "a.png"},
        {"filename": "b.png"},
        {"filename": "c.mp4"},
    ]


def test_download_output_preserves_filename_contract(monkeypatch, tmp_path):
    seen = {}

    def fake_urlopen(request, timeout):
        seen.update(request=request, timeout=timeout)
        return _Response(b"generated")

    monkeypatch.setattr(comfy.urllib.request, "urlopen", fake_urlopen)
    output = comfy.comfy_download_output(
        "http://comfy/",
        {"filename": "result.final.png", "subfolder": "folder name", "type": "output"},
        tmp_path / "images",
        4,
        "krea2",
    )

    assert output == tmp_path / "images" / "shot_004_krea2.png"
    assert output.read_bytes() == b"generated"
    assert seen["timeout"] == 120
    query = urllib.parse.parse_qs(urllib.parse.urlparse(seen["request"]).query)
    assert query == {"filename": ["result.final.png"], "subfolder": ["folder name"], "type": ["output"]}


def test_download_failure_never_exposes_partial_target(monkeypatch, tmp_path):
    target = tmp_path / "out.bin"

    def broken_urlopen(*args, **kwargs):
        raise urllib.error.URLError("connection lost")

    monkeypatch.setattr(comfy.urllib.request, "urlopen", broken_urlopen)
    with pytest.raises(urllib.error.URLError):
        comfy.comfy_download_file("http://comfy", {"filename": "out.bin"}, target)

    assert not target.exists()
    assert not list(tmp_path.glob("*.part"))


def test_download_rejects_history_item_without_filename(tmp_path):
    with pytest.raises(RuntimeError, match="no filename"):
        comfy.comfy_download_file("http://comfy", {}, tmp_path / "out.bin")


def test_upload_image_builds_multipart_and_returns_subfolder_name(monkeypatch, tmp_path):
    source = tmp_path / "source.JPG"
    source.write_bytes(b"image-body")
    seen = {}

    def fake_urlopen(request, timeout):
        seen.update(request=request, timeout=timeout)
        return _Response(json.dumps({"name": "stored.jpg", "subfolder": "inputs"}).encode())

    monkeypatch.setattr(comfy.urllib.request, "urlopen", fake_urlopen)
    uploaded = comfy.comfy_upload_image("http://comfy/", source, "shot_001")

    request = seen["request"]
    assert uploaded == "inputs/stored.jpg"
    assert request.full_url == "http://comfy/upload/image"
    assert seen["timeout"] == 120
    assert b'image-body' in request.data
    assert b'filename="shot_001.jpg"' in request.data
    assert request.get_header("Content-type").startswith("multipart/form-data; boundary=")


def test_bound_client_accepts_relative_json_paths(monkeypatch):
    seen = {}

    def fake_request(url, payload=None, timeout=30):
        seen.update(url=url, payload=payload, timeout=timeout)
        return {"ok": True}

    monkeypatch.setattr(comfy, "comfy_request_json", fake_request)
    client = comfy.ComfyClient("http://comfy///")

    assert client.request_json("system_stats", timeout=7) == {"ok": True}
    assert seen == {"url": "http://comfy/system_stats", "payload": None, "timeout": 7}


def test_workflow_compatibility_reports_missing_and_malformed_nodes():
    report = comfy.validate_workflow_compatibility(
        {
            "1": {"class_type": "KSampler", "inputs": {}},
            "2": {"class_type": "CustomMissing", "inputs": {}},
            "broken": {"inputs": {}},
        },
        {"KSampler": {}},
    )

    assert report == {
        "ok": False,
        "nodes": 3,
        "node_types": 2,
        "missing_node_types": ["CustomMissing"],
        "invalid_nodes": ["broken"],
    }


def test_repository_workflows_are_structurally_valid():
    available = {}
    loaded = {}
    for name, path in comfy.DEFAULT_WORKFLOW_PATHS.items():
        workflow = json.loads(path.read_text(encoding="utf-8"))
        loaded[name] = workflow
        for node in workflow.values():
            available[str(node["class_type"])] = {}

    for workflow in loaded.values():
        assert comfy.validate_workflow_compatibility(workflow, available)["ok"] is True


def test_gui_keeps_legacy_helper_exports():
    import content_pipeline_gui as pipeline

    assert pipeline.comfy_queue_prompt is comfy.comfy_queue_prompt
    assert pipeline.comfy_wait_for_history is comfy.comfy_wait_for_history
    assert pipeline.comfy_download_output is comfy.comfy_download_output
