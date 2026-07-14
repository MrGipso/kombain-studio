# Local model evaluation

Date: 2026-07-14  
Host: RTX 5090 32 GB, 96 GB RAM, LM Studio 0.4.19+2, llama.cpp b9851 CUDA 12.8

## Decision

Use **Gemma4-26B-A4B QAT Q4_0** as the production default for the current
LM Studio/OpenAI-compatible integration. It is the faster and more predictable
drop-in model for route selection, prompt rewriting, structured output and
basic tool calls.

Do not switch the current backend directly to **Qwen3.6-27B Q4_K_M**. Its
semantic planning is strong, but LM Studio's `/v1/chat/completions` ignored the
requested `thinking=off` setting in every measured request. Use it only after
the application moves that role to the native LM Studio Python SDK (or another
runtime where no-thinking behavior is verified).

Neither model should directly decide whether an image or video is accepted.
The model should report observable defects in a validated structure; a
deterministic policy engine should select `accept`, `needs_review` or `retry`.

## Evaluated model artifacts

Upstream model cards:

- [Qwen3.6-27B](https://huggingface.co/Qwen/Qwen3.6-27B)
- [Qwen3.6-27B LM Studio GGUF](https://huggingface.co/lmstudio-community/Qwen3.6-27B-GGUF)
- [Gemma4-26B-A4B-it](https://huggingface.co/google/gemma-4-26B-A4B-it)
- [Gemma4-26B-A4B-it official QAT GGUF](https://huggingface.co/google/gemma-4-26B-A4B-it-qat-q4_0-gguf)

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| Qwen3.6 Q4_K_M | 16,547,398,784 | `33625D8DC3A5DD8D88C324D47DB58561B11F7072816287078BFE58B4C55782F9` |
| Qwen3.6 BF16 projector | 931,145,856 | `B5D6D9C7063068CE85130BAE1D7851EADAE1F81F9ECFAE82112ADFFD4B736B45` |
| Gemma4 QAT Q4_0 | 14,439,362,752 | `9B96AA267521008235F8792590CB8E2DC47A8A236C6FF1767964CBBE32510873` |
| Gemma4 QAT BF16 projector | 1,194,827,776 | `A823A84619622B7B3132BA760E97BCF959FB13D2213CCCAEF5317DF388D35C3C` |

Both models were loaded with 32,768 context, parallelism 1 and full GPU
offload. The practical Qwen file is a community GGUF quant of the official
checkpoint; production deployments must pin the exact file hash above.

## Full application-shaped A/B

The full v2 run used 16 real project-derived cases. JSON Schema was visible to
the endpoint, route labels were removed from model-visible IDs and input, and
manual cases were excluded from automated totals. Each case was run once, so
these values are regression measurements rather than multi-seed statistics.

| Metric | Gemma4 QAT | Qwen3.6 Q4_K_M |
|---|---:|---:|
| Full-suite inference time | 26.36 s | 348.50 s |
| Median case latency | 1.71 s | 24.27 s |
| Median generation rate | 79.51 tok/s | 46.52 tok/s |
| Route policy | 4/4 | 4/4 |
| Prompt rewrite | 2/3 + one scorer false negative | 0/3 |
| Tool behavior | 2/3 | 1/3 |
| Reasoning tokens | 0 | 15,362 / 16,317 completion tokens |
| `thinking=off` ignored | 0/16 | 16/16 |

Qwen spent 94.15% of completion tokens on hidden reasoning and took 13.22x
longer overall. Several final answers and tool calls were never emitted because
the reasoning consumed the 1,200-token output budget. The same Qwen model did
call tools correctly in a separate native-SDK test with thinking disabled,
which identifies the adapter as the immediate blocker rather than tool-use
training alone.

## Vision QA findings

The v3 QA subset added an explicit action rubric and a matched/swapped image
control. It also moved disputed and culturally sensitive fixtures to manual
review.

| Metric (automated QA cases) | Gemma4 QAT | Qwen3.6 Q4_K_M |
|---|---:|---:|
| Structured contract pass rate | 4/4 | 2/4 |
| Matched city image | Correct accept | Correct accept |
| Swapped crane blueprint | Correct retry_image | Correct retry_image |
| Still-QA behavioral passes | 2/3 | 2/3 |
| `thinking=off` ignored | 0/6 | 6/6 |

Gemma also rejected the garbled-code image, but classified the defect as
`semantic_mismatch` instead of the narrower `garbled_text` category. Qwen
exhausted its reasoning budget on that image and on the equivalent tool call.

On generated videos, FFmpeg freeze detection and human review found that the
eight-second typing clip was mostly frozen. Gemma correctly requested a retry;
Qwen falsely accepted it. Gemma was safer but over-conservative on a separate
moving Hadza clip. This is why a VLM cannot be the sole gate.

## Required production changes

1. Replace prompt-only JSON in `core/lm.py` with the
   [LM Studio Python SDK](https://lmstudio.ai/docs/python): Pydantic/JSON Schema
   responses, native image input, typed tools and explicit prediction config.
2. Keep Gemma as the synchronous production model. Optionally route difficult
   planning/research cases to Qwen only through an adapter proven to disable
   thinking. The native `/api/v1/chat` supports `reasoning: "off"`, but custom
   tools require the SDK or a compatible endpoint.
3. Make vision output observational: booleans or typed fields such as
   `semantic_match`, `garbled_text`, `bad_anatomy`, `broken_geometry`,
   `motion_present` and confidence. Map those fields to actions in code.
4. Reject schema-invalid, out-of-range or contradictory responses; retry once
   with a repair prompt, then fall back to `needs_review`.
5. Run deterministic video checks first: `freezedetect`, black/blur detection,
   duration, decode integrity and sampled-frame change. Send selected frames to
   the VLM only after those checks.
6. Use one global GPU lease. Never run a 26/27B VLM and an LTX/Comfy render at
   the same time on the 32 GB card. Use `nvidia-smi` as the source of truth.
7. Change the local endpoint from the stale LAN address to
   `http://127.0.0.1:1234/v1`. Bind LM Studio and ComfyUI to loopback or enable
   firewall/API-token protection; both currently listen on `0.0.0.0`.
8. Keep `ai_frame_qa` disabled until validation and deterministic action mapping
   are implemented. The existing scalar scorer clamps a model's accidental
   0–10 score into 0–1, which can turn an invalid high value into a false accept.

## Reproduction

The default immutable suite is `tools/local_model_eval_cases.v3.json`.

```powershell
.\.venv\Scripts\python.exe tools\local_model_eval.py `
  --model <loaded-model-id> `
  --base-url http://127.0.0.1:1234/v1 `
  --thinking-mode off
```

Harness verification on this host: 40 tests passed, Ruff passed, all fixture
hashes passed. LM Studio models were unloaded after the evaluation.
