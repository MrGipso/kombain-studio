"""Render one narration batch while loading OmniVoice exactly once.

This helper is launched with the Python interpreter that owns the configured
``omnivoice-infer`` entry point.  The web backend itself therefore stays free
of the large Torch/OmniVoice dependency while a multi-chunk narration avoids
reloading the model for every sentence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import soundfile as sf
import torch

from omnivoice.models.omnivoice import OmniVoice


def _device(requested: str) -> str:
    if requested and requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _write_audio(model, output: Path, **kwargs) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    audios = model.generate(**kwargs)
    sf.write(str(output), audios[0], model.sampling_rate)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()

    request_path = Path(args.request).resolve()
    data = json.loads(request_path.read_text(encoding="utf-8"))
    model_name = str(data.get("model") or "k2-fsa/OmniVoice")
    device = _device(str(data.get("device") or "auto"))
    dtype = torch.float16 if device != "cpu" else torch.float32
    print(f"OmniVoice: loading {model_name} on {device}", flush=True)
    model = OmniVoice.from_pretrained(model_name, device_map=device, dtype=dtype)

    common = {
        "language": str(data.get("language") or "ru"),
        "num_step": int(data.get("num_step") or 32),
        "guidance_scale": float(data.get("guidance_scale") or 2.0),
        "t_shift": float(data.get("t_shift") or 0.1),
        "denoise": bool(data.get("denoise", True)),
        "postprocess_output": bool(data.get("postprocess_output", True)),
        "layer_penalty_factor": float(data.get("layer_penalty_factor") or 5.0),
        "position_temperature": float(data.get("position_temperature") or 5.0),
        "class_temperature": float(data.get("class_temperature") or 0.0),
    }

    reference = Path(str(data.get("reference_audio") or "")).resolve()
    reference_text = str(data.get("reference_text") or "").strip()
    if not reference.is_file():
        instruction = str(data.get("instruct") or "").strip()
        if not instruction:
            raise RuntimeError("reference audio and voice instruction are both missing")
        print("OmniVoice: creating stable preset reference", flush=True)
        _write_audio(
            model,
            reference,
            text=reference_text,
            instruct=instruction,
            speed=1.0,
            **common,
        )

    chunks = data.get("chunks") if isinstance(data.get("chunks"), list) else []
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue
        output = Path(str(chunk.get("output") or "")).resolve()
        print(f"OmniVoice: fragment {index}/{total}", flush=True)
        _write_audio(
            model,
            output,
            text=text,
            ref_audio=str(reference),
            ref_text=reference_text or None,
            speed=float(chunk.get("speed") or 1.0),
            **common,
        )

    print("OmniVoice: batch completed", flush=True)


if __name__ == "__main__":
    main()
