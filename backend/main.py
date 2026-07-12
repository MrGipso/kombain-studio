from __future__ import annotations

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.routers import projects, transcription, scenes, shots, assets, export_router, settings, media, diagnostics, music, sfx, script, system
from core.config import DEFAULT_SETTINGS_PATH, load_settings
from core.instance_lock import InstanceLock
from core.version import __version__


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Swallow benign ConnectionResetError noise: on Windows (Proactor loop) the
    # browser aborting a media range request (e.g. when a <video> reloads with a
    # new cache-buster) raises WinError 10054 inside asyncio's transport cleanup.
    # It's harmless — the request still completes — so we drop just that error and
    # let every other exception through to the default handler.
    import asyncio

    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(loop, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError):
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
    explicit_lock = os.environ.get("PIPELINE_INSTANCE_LOCK", "").strip()
    if explicit_lock:
        lock_paths = [Path(explicit_lock).expanduser().resolve()]
    else:
        output_base = Path(load_settings().output_base or "projects").expanduser().resolve()
        lock_paths = [
            DEFAULT_SETTINGS_PATH.with_name("pipeline-studio-server.lock").resolve(),
            output_base / ".pipeline-studio-server.lock",
        ]
    unique_lock_paths = {
        str(path).casefold(): path
        for path in lock_paths
    }
    instance_locks: list[InstanceLock] = []
    try:
        for lock_path in sorted(unique_lock_paths.values(), key=lambda value: str(value).casefold()):
            instance_locks.append(InstanceLock(lock_path).acquire())
    except Exception:
        for instance_lock in reversed(instance_locks):
            instance_lock.release()
        loop.set_exception_handler(previous)
        raise
    try:
        yield
    finally:
        for instance_lock in reversed(instance_locks):
            instance_lock.release()
        loop.set_exception_handler(previous)


app = FastAPI(title="Pipeline Studio API", version=__version__, lifespan=lifespan)

LOCAL_ORIGINS = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
}
trusted_hosts = ["localhost", "127.0.0.1", "testserver"]
trusted_hosts.extend(
    host.strip()
    for host in os.environ.get("PIPELINE_TRUSTED_HOSTS", "").split(",")
    if host.strip()
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)


@app.middleware("http")
async def reject_untrusted_mutation_origins(request: Request, call_next):
    """Reject browser writes from foreign origins while CLI clients keep working."""
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin not in LOCAL_ORIGINS:
        return JSONResponse({"detail": "Untrusted Origin"}, status_code=403)
    return await call_next(request)

# The SPA is served from the same origin as the API, so CORS is only needed for
# the Vite dev server on localhost. A wildcard origin combined with credentials
# is invalid per the CORS spec and needlessly exposes the API, so we pin to the
# local dev origins instead.
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(LOCAL_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router, prefix="/api/projects", tags=["Projects"])
app.include_router(transcription.router, prefix="/api/projects", tags=["Transcription"])
app.include_router(scenes.router, prefix="/api/projects", tags=["Scenes"])
app.include_router(shots.router, prefix="/api/projects", tags=["Shots"])
app.include_router(assets.router, prefix="/api/projects", tags=["Assets"])
app.include_router(export_router.router, prefix="/api/projects", tags=["Export"])
app.include_router(settings.router, prefix="/api", tags=["Settings"])
app.include_router(media.router, prefix="/api", tags=["Media"])
app.include_router(diagnostics.router, prefix="/api", tags=["Diagnostics"])
app.include_router(music.router, prefix="/api/projects", tags=["Music"])
app.include_router(sfx.router, prefix="/api/projects", tags=["Sfx"])
app.include_router(script.router, prefix="/api/projects", tags=["Script"])
app.include_router(system.router, prefix="/api", tags=["System"])

# Serve frontend: existing files directly, everything else -> index.html (SPA)
static_dir = Path(__file__).resolve().parent / "static"
index_path = static_dir / "index.html"


def _safe_static_path(path: str) -> Path | None:
    """Resolve a client-supplied SPA path to a real file STRICTLY inside
    static_dir, or None if it escapes. Without this, an encoded '../../..' in
    the catch-all route let FileResponse read arbitrary process files (LFI):
    the request path was joined to static_dir and served without containment.
    """
    if not path:
        return None
    try:
        candidate = (static_dir / path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    root = static_dir.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


@app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_frontend(path: str):
    # Skip API routes (they're handled by routers above)
    if path.startswith("api/") or path.startswith("static/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    resolved = _safe_static_path(path)

    # A missing *safe* route is a valid SPA route and falls through to
    # index.html.  A path that cannot be resolved inside static/ is different:
    # it is malformed or an attempted traversal and must never receive a file
    # response (including the SPA shell).
    if path and resolved is None:
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    # Try serving exact file (only if it stays inside static_dir)
    if resolved and resolved.is_file():
        return FileResponse(str(resolved))

    # Try serving as directory index (also containment-checked)
    if resolved and resolved.is_dir():
        index = resolved / "index.html"
        if index.is_file():
            return FileResponse(str(index))

    # Fallback to index.html for SPA routing
    if index_path.exists():
        return FileResponse(str(index_path))

    return JSONResponse({"detail": "Not Found"}, status_code=404)

@app.get("/", include_in_schema=False)
async def serve_root():
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    # Bind to loopback only. There is no authentication layer, so the API must
    # not be reachable from other hosts on the network. PIPELINE_HOST changes
    # the bind address, while PIPELINE_TRUSTED_HOSTS separately allow-lists Host
    # headers; neither setting adds authentication.
    host = os.environ.get("PIPELINE_HOST", "127.0.0.1")
    uvicorn.run("backend.main:app", host=host, port=8000, reload=False)
