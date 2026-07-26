"""OmniVoice CLI adapter and deterministic WAV assembly."""

from __future__ import annotations

import json
import re
import wave
from pathlib import Path
from typing import Any, Callable

from core.process import resolve_executable, run_streaming_process
from core.script_workflow import OMNIVOICE_NONVERBAL_TAGS


AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"})
BUILTIN_VOICES: tuple[dict[str, Any], ...] = (
    {"id": "studio_documentary_m", "type": "preset", "display_name": "Документальный", "language": "ru", "description": "Низкий спокойный мужской голос", "instruct": "male, middle-aged, low pitch", "calibrated_wpm": 128},
    {"id": "studio_explainer_f", "type": "preset", "display_name": "Объясняющий", "language": "ru", "description": "Ясный уверенный женский голос", "instruct": "female, young adult, moderate pitch", "calibrated_wpm": 138},
    {"id": "studio_dynamic_m", "type": "preset", "display_name": "Динамичный", "language": "ru", "description": "Энергичный молодой мужской голос", "instruct": "male, young adult, high pitch", "calibrated_wpm": 148},
)


class OmniVoiceError(RuntimeError):
    pass


def compile_omnivoice_text(text: str, provider_tags: Any = None) -> str:
    """Compile only verified OmniVoice 0.1.5 tags; ignore invented labels."""
    value = str(text or "").strip()
    value = re.sub(
        r"\[[a-z-]+\]",
        lambda match: match.group(0).casefold() if match.group(0).casefold() in OMNIVOICE_NONVERBAL_TAGS else "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s{2,}", " ", value).strip()
    requested = provider_tags if isinstance(provider_tags, list) else []
    safe: list[str] = []
    for raw in requested:
        tag = str(raw or "").strip().casefold()
        if tag in OMNIVOICE_NONVERBAL_TAGS and tag not in safe and tag not in value.casefold():
            safe.append(tag)
    return " ".join([*safe, value]).strip()


def _python_for_entrypoint(executable: str) -> str:
    entrypoint = Path(executable)
    candidates = (
        entrypoint.parent / "python.exe",
        entrypoint.parent / "python",
        entrypoint.parent.parent / "python.exe",
        entrypoint.parent.parent / "python",
    )
    return next((str(path.resolve()) for path in candidates if path.is_file()), "")


def _generation_args(options: dict[str, Any]) -> list[str]:
    return [
        "--num_step", str(int(options.get("num_step") or 32)),
        "--guidance_scale", str(float(options.get("guidance_scale") or 2.0)),
        "--denoise", str(bool(options.get("denoise", True))).lower(),
        "--postprocess_output", str(bool(options.get("postprocess_output", True))).lower(),
    ]


def discover_voice_profiles(project_dir: Path, bundled_dir: Path) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in BUILTIN_VOICES:
        profile = dict(raw)
        cached_reference = project_dir / "voices" / profile["id"] / "reference.wav"
        if cached_reference.is_file():
            profile["reference_audio"] = str(cached_reference.resolve())
            profile["reference_transcript"] = "Сегодня мы расскажем историю, в которой каждая деталь имеет значение."
        profile["scope"] = "bundled"
        profiles.append(profile)
        seen.add(str(profile["id"]))
    for scope, root in (("project", project_dir / "voices"), ("bundled", bundled_dir)):
        if not root.exists():
            continue
        for metadata_path in sorted(root.glob("*/profile.json")):
            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            profile_id = str(data.get("id") or metadata_path.parent.name)
            if not profile_id or profile_id in seen:
                continue
            reference_name = Path(str(data.get("reference_audio") or "reference.wav")).name
            reference_path = metadata_path.parent / reference_name
            instruct = str(data.get("instruct") or "").strip()
            if not reference_path.is_file() and not instruct:
                continue
            if reference_path.is_file() and reference_path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            seen.add(profile_id)
            profiles.append({
                "id": profile_id,
                "type": str(data.get("type") or ("preset" if scope == "bundled" else "clone")),
                "display_name": str(data.get("display_name") or profile_id),
                "language": str(data.get("language") or "ru"),
                "description": str(data.get("description") or ""),
                "calibrated_wpm": int(data.get("calibrated_wpm") or 135),
                "reference_transcript": str(data.get("reference_transcript") or ""),
                "reference_audio": str(reference_path.resolve()) if reference_path.is_file() else "",
                "instruct": instruct,
                "scope": scope,
            })
    return profiles


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as stream:
        return stream.getnframes() / float(stream.getframerate())


def assemble_wav(chunks: list[tuple[Path, int]], output: Path) -> list[dict[str, Any]]:
    if not chunks:
        raise OmniVoiceError("Нет аудиофрагментов для сборки")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    cursor = 0.0
    params: tuple[int, int, int, str, str] | None = None
    with wave.open(str(output), "wb") as writer:
        for index, (path, pause_ms) in enumerate(chunks):
            with wave.open(str(path), "rb") as reader:
                current = (
                    reader.getnchannels(), reader.getsampwidth(), reader.getframerate(),
                    reader.getcomptype(), reader.getcompname(),
                )
                if params is None:
                    params = current
                    writer.setnchannels(current[0])
                    writer.setsampwidth(current[1])
                    writer.setframerate(current[2])
                    writer.setcomptype(current[3], current[4])
                elif current != params:
                    raise OmniVoiceError("OmniVoice вернул фрагменты с несовместимыми WAV-параметрами")
                frames = reader.readframes(reader.getnframes())
                duration = reader.getnframes() / float(reader.getframerate())
                writer.writeframes(frames)
                start = cursor
                cursor += duration
                safe_pause = max(0, min(5000, int(pause_ms)))
                if safe_pause and params:
                    silent_frames = round(params[2] * safe_pause / 1000)
                    writer.writeframes(b"\x00" * silent_frames * params[0] * params[1])
                    cursor += safe_pause / 1000.0
                manifest.append({
                    "index": index,
                    "file": path.name,
                    "start_sec": round(start, 3),
                    "speech_end_sec": round(start + duration, 3),
                    "end_sec": round(cursor, 3),
                    "pause_after_ms": safe_pause,
                })
    return manifest


def render_plan(
    plan: dict[str, Any],
    profile: dict[str, Any],
    output_dir: Path,
    *,
    cli: str,
    model: str,
    generation: dict[str, Any] | None = None,
    log: Callable[[str], None] | None = None,
    cancel_check: Callable[[], object] | None = None,
) -> dict[str, Any]:
    executable = resolve_executable(cli, "omnivoice-infer")
    if not executable:
        raise OmniVoiceError("Не настроен omnivoice-infer")
    chunks = plan.get("chunks") if isinstance(plan.get("chunks"), list) else []
    if not chunks:
        raise OmniVoiceError("План озвучки пуст")
    options = dict(generation or {})
    options["num_step"] = max(8, min(64, int(options.get("num_step") or 32)))
    options["guidance_scale"] = max(0.5, min(6.0, float(options.get("guidance_scale") or 2.0)))
    options["denoise"] = bool(options.get("denoise", True))
    options["postprocess_output"] = bool(options.get("postprocess_output", True))
    options["device"] = str(options.get("device") or "auto")
    chunk_dir = output_dir / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    reference = Path(str(profile.get("reference_audio") or ""))
    if not reference.is_file():
        instruct = str(profile.get("instruct") or "").strip()
        if not instruct:
            raise OmniVoiceError("Аудиореференс выбранного голоса не найден")
        profile_dir = output_dir.parent / "voices" / str(profile.get("id") or "preset")
        profile_dir.mkdir(parents=True, exist_ok=True)
        reference = profile_dir / "reference.wav"
        reference_text = "Сегодня мы расскажем историю, в которой каждая деталь имеет значение."
        profile["reference_audio"] = str(reference)
        profile["reference_transcript"] = reference_text

    rendered: list[tuple[Path, int]] = []
    batch_chunks: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        if cancel_check:
            cancel_check()
        chunk_id = str(chunk.get("id") or f"chunk_{index + 1:04d}")
        if not chunk_id.replace("_", "").replace("-", "").isalnum():
            chunk_id = f"chunk_{index + 1:04d}"
        output = chunk_dir / f"{chunk_id}.wav"
        text = compile_omnivoice_text(chunk.get("text"), chunk.get("provider_tags"))
        if not text:
            continue
        output.unlink(missing_ok=True)
        batch_chunks.append({
            "text": text,
            "output": str(output.resolve()),
            "speed": float(chunk.get("speed") or 1.0),
            "emotion": str(chunk.get("emotion") or "neutral"),
            "intensity": float(chunk.get("intensity") or 0.5),
            "provider_tags": [
                str(tag).casefold() for tag in (chunk.get("provider_tags") or [])
                if str(tag).casefold() in OMNIVOICE_NONVERBAL_TAGS
            ],
        })
        rendered.append((output, int(chunk.get("pause_after_ms") or 0)))

    transcript = str(profile.get("reference_transcript") or "").strip()
    python = _python_for_entrypoint(executable)
    batch_runner = Path(__file__).resolve().parent.parent / "tools" / "omnivoice_batch.py"
    if python and batch_runner.is_file():
        request_path = output_dir / ".omnivoice-batch-request.json"
        request = {
            "model": model or "k2-fsa/OmniVoice",
            "device": options["device"],
            "language": str(profile.get("language") or "ru"),
            "num_step": options["num_step"],
            "guidance_scale": options["guidance_scale"],
            "denoise": options["denoise"],
            "postprocess_output": options["postprocess_output"],
            "reference_audio": str(reference.resolve()),
            "reference_text": transcript,
            "instruct": str(profile.get("instruct") or "").strip(),
            "chunks": batch_chunks,
        }
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        try:
            code = run_streaming_process(
                [python, str(batch_runner), "--request", str(request_path)],
                log,
                cancel_check=cancel_check,
            )
        finally:
            request_path.unlink(missing_ok=True)
        if code != 0:
            raise OmniVoiceError(
                f"Пакетная генерация завершилась с кодом {code}. Проверьте журнал OmniVoice."
            )
    else:
        # Portable fallback for a custom standalone entry point. It is slower
        # because the third-party CLI loads the model for every fragment.
        if not reference.is_file():
            if log:
                log("OmniVoice: создаётся стабильный референс готового голоса")
            command = [
                executable, "--model", model or "k2-fsa/OmniVoice",
                "--text", transcript, "--instruct", str(profile.get("instruct") or ""),
                "--output", str(reference), *_generation_args(options),
            ]
            code = run_streaming_process(command, log, cancel_check=cancel_check)
            if code != 0 or not reference.is_file():
                raise OmniVoiceError(f"Не удалось создать референс готового голоса (код {code})")
        for index, item in enumerate(batch_chunks):
            command = [
                executable, "--model", model or "k2-fsa/OmniVoice",
                "--text", item["text"], "--ref_audio", str(reference),
                "--output", item["output"], "--speed", str(item["speed"]),
                *_generation_args(options),
            ]
            if transcript:
                command.extend(["--ref_text", transcript])
            if log:
                log(f"OmniVoice: фрагмент {index + 1}/{len(batch_chunks)}")
            code = run_streaming_process(command, log, cancel_check=cancel_check)
            if code != 0:
                raise OmniVoiceError(f"OmniVoice не создал фрагмент {index + 1} (код {code})")

    missing = [path.name for path, _pause in rendered if not path.is_file()]
    if missing:
        raise OmniVoiceError(f"OmniVoice не создал файлы: {', '.join(missing[:3])}")

    narration = output_dir / "narration.wav"
    manifest_chunks = assemble_wav(rendered, narration)
    duration = wav_duration(narration)
    manifest = {
        "voice_profile_id": profile.get("id"),
        "model": model,
        "generation": options,
        "duration_sec": round(duration, 3),
        "chunks": manifest_chunks,
    }
    (output_dir / "voice_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"path": str(narration), "duration_sec": duration, "manifest": manifest}
