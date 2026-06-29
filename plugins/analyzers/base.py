from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from core.models import Scene, Shot, TranscriptSegment
from core.project import Project


@dataclass
class AnalysisResult:
    success: bool
    scenes: list[Scene] = field(default_factory=list)
    shots: list[Shot] = field(default_factory=list)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AnalyzerPlugin(ABC):
    name: str = "base"

    @abstractmethod
    def analyze_scenes(
        self,
        project: Project,
        segments: list[TranscriptSegment] | None = None,
        script_text: str | None = None,
        config: dict | None = None,
    ) -> AnalysisResult:
        ...

    @abstractmethod
    def analyze_shots(
        self,
        project: Project,
        scenes: list[Scene],
        config: dict | None = None,
    ) -> AnalysisResult:
        ...

    def validate(self) -> list[str]:
        return []
