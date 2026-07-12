import json
from pathlib import Path
from typing import Callable

from core import transcribe as transcription
from core.project import Project
from plugins.transcribers.base import TranscriberPlugin, TranscriptionResult


class WhisperCppTranscriber(TranscriberPlugin):
    name = "whisper_cpp"

    def transcribe(
        self,
        project: Project,
        audio_path: Path,
        language: str = "ru",
        log_callback: Callable[[str], None] | None = None,
    ) -> TranscriptionResult:
        log = log_callback or (lambda _: None)

        if not audio_path.exists():
            return TranscriptionResult(success=False, error=f"Audio file not found: {audio_path}")

        try:
            whisper_cli = Path(project.settings.whisper_cli)
            if not whisper_cli.exists():
                discovered = transcription.find_default_tool("whisper-cli.exe")
                whisper_cli = Path(discovered) if discovered else whisper_cli

            model_path = Path(project.settings.whisper_model)
            if not model_path.exists():
                discovered = transcription.find_default_model()
                model_path = Path(discovered) if discovered else model_path

            ffmpeg_path = Path(project.settings.ffmpeg)
            if not ffmpeg_path.exists():
                discovered = transcription.find_default_tool("ffmpeg.exe")
                ffmpeg_path = Path(discovered) if discovered else ffmpeg_path

            output_dir = project.dir / "transcription"
            output_dir.mkdir(parents=True, exist_ok=True)

            transcript_text, core_segments, json_path, srt_path = (
                transcription.transcribe_with_whisper_cpp(
                    audio_path=audio_path,
                    whisper_cli=whisper_cli,
                    model_path=model_path,
                    ffmpeg_path=ffmpeg_path,
                    language=language.strip() or "auto",
                    output_dir=output_dir,
                    log_callback=log,
                )
            )

            # Write project files
            project.write_text("transcript", transcript_text)
            seg_data = [{"start": s.start, "end": s.end, "text": s.text} for s in core_segments]
            project.write_json("transcript_segments", seg_data)

            if json_path and json_path.exists():
                project.write_json(
                    "whisper_raw",
                    json.loads(json_path.read_text(encoding="utf-8-sig")),
                )

            return TranscriptionResult(
                success=True,
                text=transcript_text,
                segments=core_segments,
                srt_path=srt_path if srt_path.exists() else None,
                json_path=json_path if json_path.exists() else None,
            )

        except Exception as exc:
            return TranscriptionResult(success=False, error=str(exc))

    def validate(self) -> list[str]:
        errors = []
        from core.config import load_settings
        s = load_settings()
        configured = Path(s.whisper_cli).expanduser() if s.whisper_cli else None
        if configured and not configured.exists():
            errors.append(f"whisper-cli path does not exist: {s.whisper_cli}")
        elif not configured and not transcription.find_default_tool("whisper-cli.exe"):
            errors.append("whisper-cli.exe not found in PATH or settings")
        return errors
