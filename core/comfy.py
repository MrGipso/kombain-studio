"""Small, reusable HTTP adapter for the ComfyUI API.

The pipeline only needs a handful of ComfyUI endpoints: queue a workflow, poll
its history, upload an input image, and download generated files.  Keeping that
transport code here avoids making non-GUI callers import the Tk application.

The module-level ``comfy_*`` functions intentionally retain the signatures and
error behaviour of the original helpers from :mod:`content_pipeline_gui`.
``ComfyClient`` is a convenient bound-URL facade for new callers.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable


DEFAULT_COMFY_URL = "http://127.0.0.1:8188"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKFLOW_PATHS = {
    "krea": PROJECT_ROOT / "workflows" / "image_krea2_turbo_t2i_api.json",
    "ideogram": PROJECT_ROOT / "workflows" / "image_ideogram4_t2i_api.json",
    "ltx": PROJECT_ROOT / "workflows" / "LTX23_GGUF_i2v_api.json",
    "ltx_draft": PROJECT_ROOT / "workflows" / "LTX23_GGUF_i2v_draft_api.json",
    "music": PROJECT_ROOT / "workflows" / "ace_step_1_5_api.json",
}


class ComfyCancelledError(RuntimeError):
    """Raised when a caller-provided cancellation check requests a stop."""


def comfy_base_url(url: str) -> str:
    """Normalize a ComfyUI base URL while preserving the local default."""

    value = url.strip().rstrip("/")
    return value or DEFAULT_COMFY_URL


def comfy_request_json(url: str, payload: dict | None = None, timeout: int = 30) -> dict:
    """Perform one JSON request using only the Python standard library."""

    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def configured_workflow_paths(settings: object) -> dict[str, Path]:
    """Resolve configured overrides while retaining server-owned defaults."""
    setting_names = {
        "krea": "comfy_krea_workflow",
        "ideogram": "comfy_ideogram_workflow",
        "ltx": "comfy_ltx_workflow",
        "ltx_draft": "comfy_ltx_draft_workflow",
        "music": "comfy_ace_workflow",
    }
    resolved: dict[str, Path] = {}
    for name, default in DEFAULT_WORKFLOW_PATHS.items():
        configured = str(getattr(settings, setting_names.get(name, ""), "") or "").strip()
        path = Path(configured).expanduser() if configured else default
        resolved[name] = path.resolve()
    reference = str(getattr(settings, "comfy_reference_workflow", "") or "").strip()
    if reference:
        resolved["reference"] = Path(reference).expanduser().resolve()
    return resolved


def validate_workflow_compatibility(
    workflow: dict,
    object_info: dict,
) -> dict:
    """Validate API-format workflow node types against a live Comfy install."""
    invalid_nodes: list[str] = []
    node_types: set[str] = set()
    for node_id, raw in workflow.items():
        if not isinstance(raw, dict) or not str(raw.get("class_type", "") or "").strip():
            invalid_nodes.append(str(node_id))
            continue
        node_types.add(str(raw["class_type"]))
    missing = sorted(node_type for node_type in node_types if node_type not in object_info)
    return {
        "ok": not invalid_nodes and not missing,
        "nodes": len(workflow),
        "node_types": len(node_types),
        "missing_node_types": missing,
        "invalid_nodes": invalid_nodes,
    }


def validate_configured_workflows(base_url: str, settings: object) -> dict:
    """Read-only live compatibility probe; never queues a Comfy prompt."""
    object_info = comfy_request_json(
        f"{comfy_base_url(base_url)}/object_info",
        timeout=20,
    )
    workflows: dict[str, dict] = {}
    for name, path in configured_workflow_paths(settings).items():
        if not path.is_file():
            workflows[name] = {
                "ok": False,
                "path": str(path),
                "error": "workflow file not found",
            }
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("workflow root must be an object")
            workflows[name] = {
                "path": str(path),
                **validate_workflow_compatibility(raw, object_info),
            }
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            workflows[name] = {"ok": False, "path": str(path), "error": str(exc)}
    return {
        "ok": all(item.get("ok") for item in workflows.values()),
        "available_node_types": len(object_info),
        "workflows": workflows,
    }


def comfy_queue_prompt(base_url: str, workflow: dict) -> str:
    """Queue an API-format workflow and return its ComfyUI prompt id."""

    payload = {"prompt": workflow, "client_id": str(uuid.uuid4())}
    result = comfy_request_json(f"{comfy_base_url(base_url)}/prompt", payload, timeout=60)
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return prompt_id: {result}")
    return prompt_id


def _queue_contains_prompt(queue: dict, key: str, prompt_id: str) -> bool:
    entries = queue.get(key, [])
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if isinstance(entry, (list, tuple)) and len(entry) > 1:
            if str(entry[1]) == prompt_id:
                return True
        elif isinstance(entry, dict) and str(entry.get("prompt_id", "")) == prompt_id:
            return True
    return False


def comfy_cancel_prompt(base_url: str, prompt_id: str) -> dict:
    """Cancel only the named ComfyUI prompt, without interrupting another client.

    Pending prompts are removed through ``/queue``. ``/interrupt`` is process-
    wide in ComfyUI, so it is used only after the live queue proves that this
    exact prompt is currently running. Transport failures are reported to the
    caller but never mask the local cancellation request.
    """

    base = comfy_base_url(base_url)
    result = {
        "prompt_id": str(prompt_id),
        "deleted": False,
        "interrupted": False,
        "error": "",
    }
    try:
        queue = comfy_request_json(f"{base}/queue", timeout=5)
        if _queue_contains_prompt(queue, "queue_pending", prompt_id):
            comfy_request_json(
                f"{base}/queue",
                {"delete": [prompt_id]},
                timeout=5,
            )
            result["deleted"] = True
            # The prompt may have moved from pending to running between the
            # queue read and delete request. Re-read before deciding whether a
            # process-wide interrupt is safe.
            queue = comfy_request_json(f"{base}/queue", timeout=5)
        if _queue_contains_prompt(queue, "queue_running", prompt_id):
            comfy_request_json(f"{base}/interrupt", {}, timeout=5)
            result["interrupted"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def comfy_wait_for_history(
    base_url: str,
    prompt_id: str,
    timeout_seconds: int = 1800,
    *,
    cancel_check: Callable[[], bool] | None = None,
    poll_interval: float = 2.0,
) -> dict:
    """Poll until a prompt has outputs, reports an error, times out, or cancels.

    ``cancel_check`` is optional so existing callers retain the exact polling
    behaviour.  It gives background workers a dependency-free cancellation hook
    without coupling this transport adapter to a particular job implementation.
    """

    deadline = time.time() + timeout_seconds
    history_url = f"{comfy_base_url(base_url)}/history/{urllib.parse.quote(prompt_id)}"
    last_error = ""
    while time.time() < deadline:
        if cancel_check is not None:
            try:
                cancelled = bool(cancel_check())
            except BaseException:
                comfy_cancel_prompt(base_url, prompt_id)
                raise
            if cancelled:
                comfy_cancel_prompt(base_url, prompt_id)
                raise ComfyCancelledError(
                    f"ComfyUI generation cancelled: {prompt_id}"
                )
        try:
            result = comfy_request_json(history_url, timeout=20)
            item = result.get(prompt_id)
            if item:
                status = item.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(json.dumps(status, ensure_ascii=False))
                if item.get("outputs"):
                    return item
        except urllib.error.URLError as exc:
            last_error = str(exc)
        time.sleep(poll_interval)
    raise TimeoutError(f"ComfyUI generation timeout. Last error: {last_error}")


def comfy_collect_outputs(history_item: dict, kinds: tuple[str, ...]) -> list[dict]:
    """Flatten selected output kinds from all workflow nodes."""

    found: list[dict] = []
    for node_output in history_item.get("outputs", {}).values():
        for key in kinds:
            for item in node_output.get(key, []):
                found.append(item)
    return found


def _output_query(item: dict) -> str:
    filename = item.get("filename", "")
    if not filename:
        raise RuntimeError(f"ComfyUI output has no filename: {item}")
    return urllib.parse.urlencode(
        {
            "filename": filename,
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        }
    )


def comfy_download_file(base_url: str, item: dict, target: Path, timeout: int = 120) -> Path:
    """Download one history output atomically to an explicitly selected path."""

    query = _output_query(item)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    try:
        with urllib.request.urlopen(f"{comfy_base_url(base_url)}/view?{query}", timeout=timeout) as response:
            with partial.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def comfy_download_output(
    base_url: str,
    item: dict,
    target_dir: Path,
    shot_index: int,
    prefix: str,
) -> Path:
    """Download a per-shot output using the pipeline's established filename."""

    target_dir = Path(target_dir)
    # Keep the original helper's side effect: the destination directory exists
    # even when ComfyUI returns a malformed output record.
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = item.get("filename", "")
    if not filename:
        raise RuntimeError(f"ComfyUI output has no filename: {item}")
    ext = Path(filename).suffix or ".bin"
    target = target_dir / f"shot_{shot_index:03d}_{prefix}{ext}"
    return comfy_download_file(base_url, item, target)


def comfy_upload_image(base_url: str, image_path: Path, name: str) -> str:
    """Upload an image as multipart/form-data and return its ComfyUI name."""

    image_path = Path(image_path)
    boundary = "----ContentPipelineBoundary" + uuid.uuid4().hex
    file_bytes = image_path.read_bytes()
    safe_name = f"{name}{image_path.suffix.lower() or '.png'}"
    parts = [
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{safe_name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
        + file_bytes
        + b"\r\n",
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'.encode("utf-8"),
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(part if isinstance(part, bytes) else part.encode("utf-8") for part in parts)
    request = urllib.request.Request(
        f"{comfy_base_url(base_url)}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.loads(response.read().decode("utf-8"))
    filename = result.get("name", safe_name)
    subfolder = result.get("subfolder", "")
    return f"{subfolder}/{filename}" if subfolder else filename


class ComfyClient:
    """Bound-URL facade over the backward-compatible transport functions."""

    def __init__(self, base_url: str = DEFAULT_COMFY_URL):
        self.base_url = comfy_base_url(base_url)

    def request_json(self, path_or_url: str, payload: dict | None = None, timeout: int = 30) -> dict:
        url = path_or_url if "://" in path_or_url else f"{self.base_url}/{path_or_url.lstrip('/')}"
        return comfy_request_json(url, payload, timeout)

    def queue_prompt(self, workflow: dict) -> str:
        return comfy_queue_prompt(self.base_url, workflow)

    def cancel_prompt(self, prompt_id: str) -> dict:
        return comfy_cancel_prompt(self.base_url, prompt_id)

    def wait_for_history(
        self,
        prompt_id: str,
        timeout_seconds: int = 1800,
        *,
        cancel_check: Callable[[], bool] | None = None,
        poll_interval: float = 2.0,
    ) -> dict:
        return comfy_wait_for_history(
            self.base_url,
            prompt_id,
            timeout_seconds,
            cancel_check=cancel_check,
            poll_interval=poll_interval,
        )

    @staticmethod
    def collect_outputs(history_item: dict, kinds: tuple[str, ...]) -> list[dict]:
        return comfy_collect_outputs(history_item, kinds)

    def download_file(self, item: dict, target: Path, timeout: int = 120) -> Path:
        return comfy_download_file(self.base_url, item, target, timeout)

    def download_output(self, item: dict, target_dir: Path, shot_index: int, prefix: str) -> Path:
        return comfy_download_output(self.base_url, item, target_dir, shot_index, prefix)

    def upload_image(self, image_path: Path, name: str) -> str:
        return comfy_upload_image(self.base_url, image_path, name)


__all__ = [
    "ComfyCancelledError",
    "ComfyClient",
    "DEFAULT_WORKFLOW_PATHS",
    "DEFAULT_COMFY_URL",
    "comfy_base_url",
    "comfy_cancel_prompt",
    "comfy_collect_outputs",
    "comfy_download_file",
    "comfy_download_output",
    "comfy_queue_prompt",
    "comfy_request_json",
    "comfy_upload_image",
    "comfy_wait_for_history",
    "configured_workflow_paths",
    "validate_configured_workflows",
    "validate_workflow_compatibility",
]
