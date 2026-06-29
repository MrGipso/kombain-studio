from __future__ import annotations

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.routers import projects, transcription, scenes, shots, assets, export_router, settings, media


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Pipeline Studio API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

# Serve frontend: existing files directly, everything else -> index.html (SPA)
static_dir = Path(__file__).resolve().parent / "static"
index_path = static_dir / "index.html"

@app.api_route("/{path:path}", methods=["GET", "HEAD"])
async def serve_frontend(path: str):
    # Skip API routes (they're handled by routers above)
    if path.startswith("api/") or path.startswith("static/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    # Try serving exact file
    if path:
        file_path = static_dir / path
        if file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path))

    # Try serving as directory index
    if path:
        dir_path = static_dir / path
        if dir_path.exists() and dir_path.is_dir():
            index = dir_path / "index.html"
            if index.exists():
                return FileResponse(str(index))

    # Fallback to index.html for SPA routing
    if index_path.exists():
        return FileResponse(str(index_path))

    return JSONResponse({"detail": "Not Found"}, status_code=404)

@app.get("/")
async def serve_root():
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
