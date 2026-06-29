from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.models import AssetCard, Shot
from core.project import Project


@dataclass
class GeneratorResult:
    success: bool
    outputs: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class GeneratorPlugin(ABC):
    name: str = "base"

    @abstractmethod
    def generate(self, project: Project, card: AssetCard, config: dict | None = None) -> GeneratorResult:
        ...

    def can_handle(self, card: AssetCard, config: dict | None = None) -> bool:
        return False

    def validate(self) -> list[str]:
        return []
