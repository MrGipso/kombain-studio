# Комбайн Studio

Local-first AI video production pipeline: script or voice recording → transcript
→ scenes → shot plan → stock/AI assets → timeline → quality control → export.

The FastAPI + React application is the only interface. It can run in a
browser or in the native window provided by `launch.py`; both modes use the
same backend, settings and orchestration. Every shipped launcher (including the
older `start_content_pipeline*.bat` and `start_pipeline_studio.bat` aliases of
`start_desktop.bat`) and `main.py` open this interface.

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
configured workflows use node types supported by the connected instance and
satisfy the engine's marker contract, without queueing a render.

## Custom ComfyUI workflows

Every ComfyUI workflow (Krea, Ideogram, LTX, LTX draft, MiniMax H3, ACE-Step,
canonical reference) can be replaced by your own API-format export, including workflows
built from ComfyUI's cloud API nodes, without code changes. Mark the nodes the
engine fills by putting markers in their titles (`_meta.title`), for example
`ENGINE_PROMPT`, `ENGINE_INPUT_IMAGE`, `ENGINE_DURATION` or `ENGINE_OUTPUT`.
Workflows without markers fall back to the bundled node ids. Cloud
(partner/API) nodes need a comfy.org API key: set it in Settings
(`comfy_api_key`) and the engine sends it with every queued prompt as
`extra_data.api_key_comfy_org`. The roles per workflow, the syntax and the
notes on cloud nodes are documented in
[docs/COMFY_WORKFLOWS.md](docs/COMFY_WORKFLOWS.md).

AI shots are animated by LTX 2.3 by default. Settings → *Видео-движок* switches
image-to-video to MiniMax H3 (open weights in the same local ComfyUI, 768p,
5–15 s clips). For H3 the engine builds the prompt in the structured format of
MiniMax's official prompt-writing guide, converts the slot length to H3's
frame grid, picks the render size from the shot image and asks ComfyUI to free
VRAM when switching to or from H3. The draft mode uses H3's 8-step turbo LoRA.

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

A cloud LLM is an explicit opt-in: set **Провайдер LLM** to *OpenAI-совместимый
API*, point the URL at the provider's `/v1` base (for example
`https://api.openai.com/v1` or `https://openrouter.ai/api/v1`) and save the API
key. The key is stored like the stock keys (never returned by the settings API)
and is sent as a Bearer token only to the configured URL's origin, only in that
mode. Cloud mode skips LM Studio's TTL, model load/unload and SDK transport and
requests non-strict JSON Schema output, which the client validates and repairs
locally. Script text, scenes and stock previews then leave the machine, and an
unattended run makes hundreds of billable requests; scene segmentation also
needs a provider with an `/embeddings` endpoint.

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
- `tests/` — offline unit and API-oriented tests
- `workflows/` — ComfyUI API workflows

Machine-specific settings, generated projects, models, logs, and built frontend
assets are intentionally ignored by Git.

## License

Pipeline Studio is released under the [MIT License](LICENSE). External tools
and models it drives (LM Studio, ComfyUI and its models, whisper.cpp, FFmpeg,
OmniVoice, Woosh, stock providers) are installed separately and keep their own
licenses and terms of use.
