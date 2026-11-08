"""Hand the GPU from ComfyUI to LM Studio before an LLM job.

ComfyUI keeps its models in VRAM after a render (Krea + LTX can hold 24 GB of
a 32 GB card). LM Studio then cannot load the chat model, and every LLM stage
fails with an opaque "Failed to load model" — scene labels silently degrade,
shot planning aborts. The two engines never work at the same time in this
application (``llm`` and ``comfy`` jobs share the ``gpu`` lease), so when an
LLM job starts it is safe to ask ComfyUI to unload everything.

Only done when both services run on the same machine: a remote LM Studio or
a cloud LLM does not compete for this GPU.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from core import lm
from core.comfy import DEFAULT_COMFY_URL, comfy_free_memory, comfy_vram_reserved
from core.config import load_settings

log = logging.getLogger(__name__)

# Below this ComfyUI holds no model weights worth unloading (its idle CUDA
# context and allocator cache stay under a gigabyte).
IDLE_VRAM_BYTES = 1 << 30
WAIT_SECONDS = 30.0
POLL_SECONDS = 0.5


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").strip().lower()


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def same_machine(lm_url: str, comfy_url: str) -> bool:
    lm_host, comfy_host = _host(lm_url), _host(comfy_url)
    if not lm_host or not comfy_host:
        return False
    if _is_loopback(lm_host) and _is_loopback(comfy_host):
        return True
    return lm_host == comfy_host


def release_comfy_for_llm(
    settings: Any = None,
    *,
    vram_reserved: Callable[[str], int | None] = comfy_vram_reserved,
    free_memory: Callable[[str], bool] = comfy_free_memory,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Unload ComfyUI models if they occupy the GPU LM Studio is about to use.

    Returns a short status for logs and tests: ``skipped-*``, ``idle``,
    ``freed``, ``timeout`` or ``failed``. Never raises.
    """

    settings = settings if settings is not None else load_settings()
    if not lm.is_lmstudio(settings):
        return "skipped-cloud"
    comfy_url = (getattr(settings, "comfy_url", "") or DEFAULT_COMFY_URL).strip()
    if not same_machine(lm.base_url(settings), comfy_url):
        return "skipped-remote"
    held = vram_reserved(comfy_url)
    if held is None:
        return "skipped-offline"
    if held < IDLE_VRAM_BYTES:
        return "idle"
    log.info("Освобождаю видеопамять ComfyUI (%.1f ГБ) для языковой модели", held / 2**30)
    if not free_memory(comfy_url):
        log.warning("ComfyUI не принял запрос на освобождение видеопамяти")
        return "failed"
    # /free only sets a flag; ComfyUI's worker unloads asynchronously.
    deadline = clock() + WAIT_SECONDS
    while clock() < deadline:
        sleep(POLL_SECONDS)
        held = vram_reserved(comfy_url)
        if held is None or held < IDLE_VRAM_BYTES:
            return "freed"
    log.warning("ComfyUI не освободил видеопамять за %.0f с", WAIT_SECONDS)
    return "timeout"


def install() -> Callable[[], None]:
    """Register the hook for every ``llm`` job; returns the remover."""
    from backend.services import jobs

    return jobs.add_acquire_hook("llm", lambda: release_comfy_for_llm())
