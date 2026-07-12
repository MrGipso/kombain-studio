# Pipeline Studio — release checklist

## 1. Legal and source review

- Select the licence for Pipeline Studio and add the approved root `LICENSE`.
  The repository intentionally does not choose one automatically.
- Review `docs/THIRD_PARTY.md`, asset credits and the immutable Master manifest.
- Disable Woosh and every other non-commercial integration in commercial builds.
- Record the source revision and confirm the working tree contains only intended
  release changes.

## 2. Reproducible dependency inventory

From the repository root, run:

```powershell
python tools/generate_sbom.py --output artifacts/pipeline-studio.cdx.json
```

The command is a Python-standard-library-only, offline generator. It reads
`pyproject.toml`, `uv.lock` and `frontend/package-lock.json`, then atomically
writes CycloneDX 1.5 JSON. Components are sorted, npm SRI values are converted
to hexadecimal hashes, and Python source-archive hashes come from `uv.lock`.
The document contains no wall-clock timestamp; its UUID is derived from the
exact input bytes, so unchanged lockfiles produce byte-identical output.

The SBOM covers locked Python and npm packages only. Before distribution, add a
separate signed inventory for external executables, services and model weights
(including FFmpeg, whisper.cpp, LM Studio, ComfyUI and any local models), with
their versions, hashes, licences and acquisition sources.

The lockfiles do not provide complete licence metadata for every Python
distribution. Generate and review a separate dependency-licence report against
the exact locked environment; do not treat the CycloneDX inventory as legal
clearance by itself.

## 3. Verification and packaging

Run the passive local integration preflight for release-critical tooling:

```powershell
.\.venv\Scripts\python.exe tools\integration_smoke.py --targets ffmpeg whisper comfy --require ffmpeg whisper comfy --output artifacts\integration-smoke.json
```

Supported targets are `ffmpeg`, `whisper`, `comfy`, `lm`, `embed`, and `woosh`.
Passive mode is the default. It uses the configured local paths and endpoints
without starting services, submitting inference requests, triggering LM Studio
JIT model loading, modifying projects, or submitting generation jobs. Passive
`lm` and `embed` checks read `/v1/models`; they verify availability and model
residency, not inference. The ComfyUI smoke reads `/object_info` and validates
node compatibility for all five bundled workflows without queueing a render.

After freeing enough VRAM and while no render is running, verify LM inference
explicitly when it is part of the release acceptance scope:

```powershell
.\.venv\Scripts\python.exe tools\integration_smoke.py --targets lm embed --require lm embed --active --output artifacts\integration-smoke-active.json
```

`--active` sends minimal chat and embedding requests. It may JIT-load the
configured models, consume substantial VRAM, and contend with ComfyUI. The
requests include a short TTL, but release operators must still check model
residency if GPU memory needs to be reclaimed immediately.

Before recording RC evidence, verify that LM Studio binds only to `127.0.0.1`,
the application uses `http://127.0.0.1:1234/v1`, and CORS plus sensitive
request/prompt logging are disabled. Do not accept an RC with the
unauthenticated LM endpoint exposed to a LAN, overlay network, or the public
Internet.

Exit status `0` means every target named by `--require` passed; failures of
targets that were checked but not required are informational. Status `1` means
one or more required targets failed, and status `2` reports invalid CLI
arguments. Archive the applicable passive and active smoke reports with the
release evidence when the external tools form part of the supported
distribution.

Run the isolated Windows golden acceptance:

```powershell
.\.venv\Scripts\python.exe tools\golden_e2e.py
```

This uses only configured FFmpeg/Whisper plus a synthetic System.Speech fixture.
It starts a loopback-only backend with separate settings, projects, jobs and
instance lock, forces the production offline LM fallbacks, and proceeds through
Master/delivery. Accept the run only when `golden-report.json` records all seven
stages as `completed`, exactly four durable jobs as `succeeded`, zero validation
errors, successful technical QC, matching download hashes, one immutable asset
variant and a verified Master index/contract fingerprint. Archive the entire
reported run directory as RC evidence.

Run the full local release gate:

```powershell
check.bat
```

This verifies `uv.lock`, regenerates the SBOM, runs the Python test suite and
builds/lints the frontend. CI publishes `pipeline-studio-sbom` as a workflow
artifact from the Python 3.12 job. Attach that JSON document, required notices,
the external/model inventory and checksums of distributed binaries to the
release. Do not publish a commercial package while any non-commercial model or
asset is enabled.
