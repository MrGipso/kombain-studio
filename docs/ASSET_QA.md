# Asset QA architecture

The web pipeline uses a fail-safe QA layer for newly produced images and
videos. It is intentionally not an autonomous render agent: no QA code invokes
ComfyUI, LTX, stock search, or another generation attempt.

## Decision boundary

The vision model returns only `VisionObservations`: semantic alignment,
composition, expected-text legibility, bounded confidence, and typed visible
findings. Pydantic rejects extra fields, invalid enums, duplicate findings and
confidence outside `0..1`. The model cannot return `accept`, `retry`, `review`
or a quality score.

`core.asset_qa` maps observations to one of:

- `accept`
- `needs_review`
- `retry_image`
- `retry_video`

The last two are recommendations. The application never executes them
automatically. A schema-invalid response gets exactly one repair attempt; a
second invalid response or a transport failure becomes `needs_review`.

The legacy stock selector now also validates its `0..1` score through JSON
Schema. An accidental `5/10` response fails closed at `0.0` instead of being
clamped to a false-perfect `1.0`.

## Deterministic evidence

Images are verified and decoded with Pillow. Videos are checked with FFprobe
and FFmpeg for:

- a real video stream, dimensions, FPS and positive duration;
- duration drift from the shot slot;
- full decode integrity (`-xerror`);
- black intervals (`blackdetect`);
- frozen intervals (`freezedetect`).

Detector intervals are clamped, merged and converted to covered fractions.
Technical decode/duration failures can recommend a retry without consulting an
LLM. Freeze thresholds are route-aware so a deliberate still/Ken Burns shot is
not judged as dynamic i2v.

## Evidence store

Reports are append-only in each project's `.asset_qa.json`. Every report is
bound to `card_id`, media kind and SHA-256 of the exact inspected bytes. The API
marks a report stale when the current board path points at different bytes.
Base64 image data is never stored.

Endpoints:

- `GET /api/projects/{project}/asset-board/qa` — latest report per card/kind;
- `POST /api/projects/{project}/asset-board/{index}/qa` — explicit recheck of
  current media with board revision and stable-card guards.

Preview includes current non-accepted reports in its weak-spots list. Stale
reports are ignored.

## Rollout modes

`ai_frame_qa` remains the master switch. `ai_qa_mode` controls application of
the deterministic recommendation:

- `shadow` (default): save and display evidence only; never change card status,
  approval or the Master export gate;
- `review`: save evidence and mark a non-accepted card `needs_review`; still no
  regeneration.

Changing QA settings does not invalidate generated assets. Start with shadow,
review the false-positive/false-negative distribution, tune policy thresholds,
then enable review mode.

## LM Studio and GPU safety

Structured text calls prefer the optional scoped LM Studio Python SDK
(`uv sync --extra local-ai`). The OpenAI-compatible HTTP transport remains the
fallback and is currently the explicit transport for image inputs. Both paths
apply the same Pydantic contract.

Web jobs with resource subtype `llm`, `comfy`, `stock` or `gpu` share one
process-wide GPU/VRAM lease while retaining their subtype in job history.
FFmpeg remains independent. This prevents a 26/27B model and Comfy/LTX from
allocating the RTX 5090 concurrently inside the supported single-process web
deployment.

The current lease is process-local. A future multi-worker or multi-host
deployment requires a durable queue/distributed lease; do not work around the
single-instance server guard.

## Remaining calibration work

- Run shadow mode over a representative production corpus and label errors.
- Add sampled-frame temporal VLM observations only after deterministic checks.
- Move native vision input to the Python SDK when its deployed version is
  proven against the configured VLM.
- Add user-approved, queue-backed retry execution as a separate capability.
  QA recommendations must not call generation recursively.
