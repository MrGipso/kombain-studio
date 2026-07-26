"""MOSS-SoundEffect v2 shim server — drop-in replacement for the Woosh SFX API.

Exposes the same minimal contract that ``core/sfx.py`` relies on:

    GET  /ping      -> {"status": "ok"}
    POST /generate  -> FLAC bytes (audio/flac)

Request-body compatibility: accepts the exact Woosh payload and maps

    args.prompt   -> prompt
    args.seed     -> deterministic seed (preserves per-query reproducible takes)
    args.seconds  -> optional duration override (new, 1..30s; Woosh never sends it)

All other Woosh sampler fields (sampler, sigma_*, S_*, num_steps, ...) are
accepted and ignored — MOSS has its own flow-matching solver.

To use: run this server (see start_moss_sfx.bat) and point ``woosh_url`` in
Комбайн Studio settings at http://127.0.0.1:8510 . Nothing else changes:
query caching, fresh takes and the export bed keep working as before.

Environment:
    MOSS_SFX_MODEL_DIR  HF repo id or local dir (default OpenMOSS-Team/MOSS-SoundEffect-v2.0)
    MOSS_SFX_SECONDS    default clip length, seconds (default 10, max 30)
    MOSS_SFX_STEPS      num_inference_steps (default 100; lower = faster, verify quality)
    MOSS_SFX_CFG        cfg_scale (default 4.0)
    MOSS_SFX_DEVICE     cuda / cpu (default cuda)
    MOSS_SFX_PORT       listen port (default 8510)
    MOSS_SFX_IDLE_UNLOAD  free VRAM after N idle seconds (default 180, 0 = keep loaded)

Model license: Apache-2.0 (no non-commercial restriction, unlike Woosh weights).
"""

from __future__ import annotations

import inspect
import io
import logging
import os
import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

log = logging.getLogger("moss-sfx")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MODEL_DIR = os.environ.get("MOSS_SFX_MODEL_DIR", "OpenMOSS-Team/MOSS-SoundEffect-v2.0")
DEFAULT_SECONDS = float(os.environ.get("MOSS_SFX_SECONDS", "10"))
MAX_SECONDS = 30.0
STEPS = int(os.environ.get("MOSS_SFX_STEPS", "100"))
CFG = float(os.environ.get("MOSS_SFX_CFG", "4.0"))
DEVICE = os.environ.get("MOSS_SFX_DEVICE", "cuda")
# Free the pipeline from VRAM after this many idle seconds (0 disables).
# Same philosophy as Комбайн Studio's LM Studio auto-unload: SFX generation
# is bursty, and an idle model must not starve ComfyUI (Krea/LTX) of VRAM.
IDLE_UNLOAD_SECONDS = int(os.environ.get("MOSS_SFX_IDLE_UNLOAD", "180"))
FALLBACK_SAMPLE_RATE = 48000  # per the v2.0 model card

_pipe = None
_pipe_lock = threading.Lock()  # one GPU generation at a time; also guards lazy load
_idle_timer: threading.Timer | None = None


def _unload_pipeline() -> None:
    global _pipe
    with _pipe_lock:
        if _pipe is None:
            return
        log.info("Idle for %ss — unloading pipeline to free VRAM", IDLE_UNLOAD_SECONDS)
        _pipe = None
        try:
            import gc

            import torch

            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 - best effort
            pass
        log.info("Pipeline unloaded; next request will reload it")


def _touch_idle_timer() -> None:
    """(Re)start the idle-unload countdown after each served request."""
    global _idle_timer
    if IDLE_UNLOAD_SECONDS <= 0:
        return
    if _idle_timer is not None:
        _idle_timer.cancel()
    _idle_timer = threading.Timer(IDLE_UNLOAD_SECONDS, _unload_pipeline)
    _idle_timer.daemon = True
    _idle_timer.start()


def _load_pipeline():
    """Lazy-load so /ping answers instantly while weights load on first request."""
    global _pipe
    if _pipe is None:
        import torch
        from moss_soundeffect_v2 import MossSoundEffectPipeline

        log.info("Loading MOSS-SoundEffect pipeline from %s ...", MODEL_DIR)
        started = time.time()
        _pipe = MossSoundEffectPipeline.from_pretrained(
            MODEL_DIR, torch_dtype=torch.bfloat16, device=DEVICE
        )
        log.info("Pipeline ready in %.1fs", time.time() - started)
    return _pipe


class GenerateArgs(BaseModel):
    # Woosh sends many sampler-specific fields; ignore anything we don't map.
    model_config = ConfigDict(extra="ignore")

    prompt: str = ""
    seed: Optional[int] = None
    seconds: Optional[float] = None


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    args: GenerateArgs


app = FastAPI(title="MOSS-SoundEffect v2 shim (Woosh-compatible SFX API)")


@app.get("/ping")
def ping():
    return {"status": "ok", "provider": "moss-soundeffect-v2", "model": MODEL_DIR}


def _seed_kwargs(pipe, seed: int) -> dict:
    """Pass the seed the way this pipeline version supports (best effort)."""
    import torch

    try:
        params = inspect.signature(pipe.__call__).parameters
    except (TypeError, ValueError):
        params = {}
    if "seed" in params:
        return {"seed": seed}
    if "generator" in params:
        generator = torch.Generator(device="cpu").manual_seed(seed)
        return {"generator": generator}
    # Fallback: global seeding still makes runs reproducible in practice.
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    return {}


def _to_flac_bytes(audio, sample_rate: int) -> bytes:
    import numpy as np
    import soundfile as sf

    x = audio
    if hasattr(x, "detach"):  # torch.Tensor without importing torch here
        x = x.detach().to("cpu").float().numpy()
    x = np.asarray(x, dtype="float32")
    if x.ndim == 3:  # (B, C, T) -> first item
        x = x[0]
    if x.ndim == 1:  # (T,) -> (1, T)
        x = x[None, :]
    x = np.clip(x, -1.0, 1.0)
    buf = io.BytesIO()
    sf.write(buf, x.T, sample_rate, format="FLAC", subtype="PCM_16")
    return buf.getvalue()


@app.post("/generate")
def generate(req: GenerateRequest):
    prompt = (req.args.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Empty prompt")
    seconds = req.args.seconds if req.args.seconds else DEFAULT_SECONDS
    seconds = max(1.0, min(float(seconds), MAX_SECONDS))

    with _pipe_lock:
        try:
            pipe = _load_pipeline()
        except Exception as exc:  # noqa: BLE001 - report load failures to the client
            log.exception("Pipeline load failed")
            raise HTTPException(503, f"Model not available: {exc}")

        kwargs = {
            "prompt": prompt,
            "seconds": seconds,
            "num_inference_steps": STEPS,
            "cfg_scale": CFG,
        }
        if req.args.seed is not None:
            kwargs.update(_seed_kwargs(pipe, int(req.args.seed) % (2**32)))

        log.info("Generating %.1fs for prompt: %.80s", seconds, prompt)
        started = time.time()
        try:
            audio = pipe(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surface as a clean HTTP error
            log.exception("Generation failed")
            raise HTTPException(500, f"Generation failed: {exc}")
        log.info("Done in %.1fs", time.time() - started)
    _touch_idle_timer()

    sample_rate = int(
        getattr(pipe, "sampling_rate", None)
        or getattr(pipe, "sample_rate", None)
        or FALLBACK_SAMPLE_RATE
    )
    try:
        payload = _to_flac_bytes(audio, sample_rate)
    except Exception as exc:  # noqa: BLE001
        log.exception("FLAC encode failed")
        raise HTTPException(500, f"FLAC encode failed: {exc}")
    if not payload:
        raise HTTPException(500, "Empty audio result")
    return Response(content=payload, media_type="audio/flac")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MOSS_SFX_PORT", "8510")))
