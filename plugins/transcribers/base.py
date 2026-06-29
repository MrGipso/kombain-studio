from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.models import TranscriptSegment
from core.project import Project


@dataclass
class TranscriptionResult:
    success: bool
    text: str = ""
    segments: list[TranscriptSegment] = field(default_factory=list)
    srt_path: Path | None = None
    json_path: Path | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class TranscriberPlugin(ABC):
    name: str = "base"

    @abstractmethod
    def transcribe(
        self,
        project: Project,
        audio_path: Path,
        language: str = "ru",
        log_callback: Callable[[str], None] | None = None,
    ) -> TranscriptionResult:
        ...

    def validate(self) -> list[str]:
        return []
