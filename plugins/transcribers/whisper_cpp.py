import tempfile
from pathlib import Path
from typing import Callable

import content_pipeline_gui as pipeline
from core.models import TranscriptSegment as CoreSegment
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
                whisper_cli = Path(pipeline.find_default_tool("whisper-cli.exe")) if pipeline.find_default_tool("whisper-cli.exe") else whisper_cli

            model_path = Path(project.settings.whisper_model)
            if not model_path.exists():
                model_path = Path(pipeline.find_default_model()) if pipeline.find_default_model() else model_path

            ffmpeg_path = Path(project.settings.ffmpeg)
            if not ffmpeg_path.exists():
                ffmpeg_path = Path(pipeline.find_default_tool("ffmpeg.exe")) if pipeline.find_default_tool("ffmpeg.exe") else ffmpeg_path

            output_dir = project.dir / "transcription"
            output_dir.mkdir(parents=True, exist_ok=True)

            log("Converting audio to 16 kHz mono WAV")
            with tempfile.TemporaryDirectory(prefix="content-pipeline-") as temp_root:
                wav_path = Path(temp_root) / "voice_16khz.wav"
                ffmpeg_command = [
                    str(ffmpeg_path),
                    "-y", "-i", str(audio_path),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                    str(wav_path),
                ]
                code = pipeline.run_process(ffmpeg_command, log)
                if code != 0:
                    return TranscriptionResult(success=False, error="ffmpeg conversion failed")

                output_base = output_dir / "whisper_transcript"
                log("Running whisper.cpp")
                whisper_command = [
                    str(whisper_cli),
                    "-m", str(model_path),
                    "-f", str(wav_path),
                    "-l", language.strip() or "ru",
                    "-otxt", "-osrt", "-oj",
                    "-of", str(output_base),
                ]
                code = pipeline.run_process(whisper_command, log)
                if code != 0:
                    return TranscriptionResult(success=False, error="whisper.cpp transcription failed")

            txt_path = output_base.with_suffix(".txt")
            json_path = output_base.with_suffix(".json")
            srt_path = output_base.with_suffix(".srt")

            transcript_text = ""
            if txt_path.exists():
                transcript_text = txt_path.read_text(encoding="utf-8", errors="replace").strip()

            raw_segments: list = []
            if json_path.exists():
                raw_segments = pipeline.parse_whisper_json(json_path)
            if not raw_segments and srt_path.exists():
                raw_segments = pipeline.parse_srt_segments(srt_path)

            core_segments = [
                CoreSegment(start=s.start, end=s.end, text=s.text)
                for s in raw_segments
            ]

            # Write project files
            project.write_text("transcript", transcript_text)
            seg_data = [{"start": s.start, "end": s.end, "text": s.text} for s in core_segments]
            project.write_json("transcript_segments", seg_data)

            if json_path and json_path.exists():
                import json
                project.write_json("whisper_raw", json.loads(json_path.read_text("utf-8")))

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
        if not pipeline.find_default_tool("whisper-cli.exe"):
            errors.append("whisper-cli.exe not found in PATH or settings")
        from core.config import load_settings
        s = load_settings()
        if s.whisper_cli and not Path(s.whisper_cli).exists():
            errors.append(f"whisper-cli path does not exist: {s.whisper_cli}")
        return errors
