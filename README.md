# Комбайн Studio

Local-first AI video production pipeline: script or voice recording → transcript
→ scenes → shot plan → stock/AI assets → timeline → quality control → export.

The FastAPI + React application is the single supported interface. It can run
in a browser or in the native window provided by `launch.py`; both modes use
the same backend, settings and orchestration. Historical Qt/Tk sources remain
temporarily in the repository for migration reference, but all shipped
launchers and `main.py` open the current interface.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Node.js and npm
- FFmpeg / FFprobe
- whisper.cpp and a compatible model
- LM Studio (scene and shot planning)
- yt-dlp (subtitle-only YouTube intake for the Script → Rewrite workflow)
- OmniVoice (`omnivoice-infer`) for chunked local narration from saved or cloned voices
- ComfyUI (image, video, and optional music generation)
- Woosh API (optional SFX generation)

External services are not installed or started automatically. Configure their
URLs and executable paths on the Settings page after the first launch.

The primary **Создать все материалы** action is a server-owned unattended run:
it freezes the approved plan, skips ready media, validates generated files,
retries each failed provider operation once, then fills the slot with a
Ken Burns or still fallback. The browser may be closed and a checkpoint is
resumed on the next application start. See
[`docs/UNATTENDED_MATERIALS.md`](docs/UNATTENDED_MATERIALS.md).

Legacy generated-media QA remains API-compatible and is documented in
[`docs/ASSET_QA.md`](docs/ASSET_QA.md), but it is not invoked by generation
from the current interface and cannot block the unattended queue.

> Licensing: the optional Woosh model weights are non-commercial. See
> `docs/THIRD_PARTY.md` before distributing or monetizing generated work.

The Windows launchers keep mutable application state in
`%LOCALAPPDATA%\PipelineStudio` (`settings.json` and `jobs.sqlite3`). Override
`PIPELINE_DATA_DIR`, `PIPELINE_SETTINGS_PATH` or `PIPELINE_JOBS_DB` for a
portable or managed installation. A legacy root settings file is copied once
on first launch; it is not deleted automatically.

## Install

```powershell
uv sync --locked --extra dev --extra local-ai
cd frontend
npm ci
cd ..
```

`local-ai` installs the scoped LM Studio Python SDK used for native structured
text calls. It may be omitted on a minimal headless install; the validated
OpenAI-compatible HTTP fallback remains available.

## Run the web application

Build the frontend and start the local server:

```powershell
.\start_web_server.bat
```

For subsequent starts without rebuilding the frontend:

```powershell
.\start_fast.bat
```

Operational probes are available at `/api/health`, `/api/readiness` and
`/api/build-info`.

The API intentionally binds to `127.0.0.1`. There is no multi-user
authentication layer yet; do not expose it to a LAN or the public Internet.
The server also rejects untrusted `Host` headers and cross-origin browser
mutations. `PIPELINE_TRUSTED_HOSTS` is an explicit deployment escape hatch, not
a substitute for authentication.

The local runtime deliberately permits one API process per settings directory
and canonical `output_base`. Cross-process instance locks make a second Uvicorn
worker/server fail closed even if it uses another settings file but points at
the same projects, because project locks are process-local. A future LAN/SaaS
deployment must use a durable worker queue and distributed leases rather than
disabling this guard.

## Development checks

```powershell
uv run --extra dev pytest
cd frontend
npm run lint
npm run build
```

## Local integration preflight

Run the passive dependency smoke before a production session or release:

```powershell
.\.venv\Scripts\python.exe tools\integration_smoke.py --targets ffmpeg whisper comfy --require ffmpeg whisper comfy
```

The available targets are `ffmpeg`, `whisper`, `comfy`, `lm`, `embed`, and
`woosh`. Passive mode is the default. Checks run concurrently against the paths
and endpoints in local settings; they do not start services, submit inference
requests, trigger LM Studio JIT model loading, modify projects, or queue
generation jobs. Passive `lm` and `embed` checks inspect `/v1/models`; they
confirm reachability and that the configured models are loaded, but do not
prove inference. The ComfyUI check reads `/object_info` and verifies that all
five bundled workflows use node types supported by the connected instance,
without queueing a render.

Use active mode only when chat and embedding inference must be verified:

```powershell
.\.venv\Scripts\python.exe tools\integration_smoke.py --targets lm embed --require lm embed --active
```

`--active` sends small `/chat/completions` and `/embeddings` requests. LM Studio
may JIT-load the configured models, allocate substantial VRAM, and contend with
ComfyUI; free enough VRAM first and do not run this probe during a render. The
requests carry a short TTL, but operators should still verify model residency
after the probe when GPU memory is release-critical.

For an RC, the LM Studio endpoint is local-only: bind it to `127.0.0.1`, use
`http://127.0.0.1:1234/v1` in application settings, and disable CORS and
sensitive request/prompt logging. Do not expose the unauthenticated endpoint to
a LAN, overlay network, or the public Internet.

Use `--output artifacts\integration-smoke.json` to atomically save the same
structured report printed to stdout.

Exit status `0` means every target listed in `--require` is available (failures
of non-required targets remain informational), `1` means at least one required
target failed, and `2` means the command-line arguments are invalid.

## Golden release acceptance

On Windows, run the isolated end-to-end acceptance before cutting an RC:

```powershell
.\.venv\Scripts\python.exe tools\golden_e2e.py
```

It creates a fresh evidence directory under `.pytest-tmp`, a separate settings
file, project root, jobs database and instance lock, then exercises the public
HTTP/SSE workflow from a synthetic voice fixture through a verified
Master/delivery export. It requires the configured Whisper and FFmpeg tools but
does not require LM Studio, ComfyUI, stock providers or Woosh. Existing settings
and projects are read-only. Successful evidence includes `golden-report.json`,
SSE transcripts, validation/job snapshots, server logs and downloaded Master
artifacts with independently checked hashes.

## Project artifacts

Projects live under the configured `output_base` directory. Each project keeps
human-readable JSON artifacts for its pipeline stages and a versioned pipeline
manifest for freshness/dependency state. Generated media is stored inside the
project so media serving remains confined to the projects root.

The principal stages are:

1. script / voice input
2. transcription
3. scene analysis
4. shot planning
5. asset generation and review
6. timeline, music, and SFX
7. validation
8. draft or master export

Shot planning also compiles `03_context_bible.json`: a versioned map of story
threads, recurring subjects, locations, continuity groups and per-shot
`before → action → after` state. The Material preflight freezes and validates
that context before any expensive generation. See
[`docs/CONTEXT_STORYBOARD.md`](docs/CONTEXT_STORYBOARD.md).

Draft exports are intended for iteration. Master exports apply stricter quality
gates and should contain only reviewed, final media. Every successful Master
also receives an immutable `*.mp4.manifest.json` with the output/profile/QC
contract, selected asset hashes, stock rights metadata, and voice/music/SFX
input hashes. An append-only chained integrity index anchors every sidecar hash;
tampered MP4 or manifest files are not offered as verified downloads. The audit
record can be downloaded from Preview alongside the MP4.

## Repository layout

- `backend/` — FastAPI application and job orchestration
- `frontend/` — React + TypeScript web client
- `core/` — domain models, project storage, validation, and export engine
- `plugins/` — transcriber, analyzer, and generator adapters
- `tests/` — offline unit and API-oriented tests
- `workflows/` — ComfyUI API workflows
- `ui/` — compatibility Qt interface

Machine-specific settings, generated projects, models, logs, and built frontend
assets are intentionally ignored by Git.
