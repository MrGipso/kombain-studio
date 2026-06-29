from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.project import Project


@dataclass
class PipelineStep:
    name: str
    status: str = "pending"
    progress: float = 0.0
    error: str | None = None
    output: Any = None


@dataclass
class PipelineContext:
    project: Project
    steps: dict[str, PipelineStep] = field(default_factory=dict)
    callbacks: dict[str, list[Callable]] = field(default_factory=dict)

    def __post_init__(self):
        self.steps = {
            "transcription": PipelineStep(name="Transcription"),
            "scene_analysis": PipelineStep(name="Scene Analysis"),
            "shot_list": PipelineStep(name="Shot List"),
            "asset_board": PipelineStep(name="Asset Board"),
            "generation": PipelineStep(name="Generation"),
            "export": PipelineStep(name="Export"),
        }

    def on(self, event: str, callback: Callable):
        self.callbacks.setdefault(event, []).append(callback)

    def emit(self, event: str, **data):
        for cb in self.callbacks.get(event, []):
            cb(**data)

    def set_status(self, step: str, status: str, error: str | None = None):
        if step in self.steps:
            self.steps[step].status = status
            self.steps[step].error = error
            self.emit("step_status", step=step, status=status, error=error)

    def set_progress(self, step: str, progress: float):
        if step in self.steps:
            self.steps[step].progress = progress
            self.emit("step_progress", step=step, progress=progress)

    def set_output(self, step: str, output: Any):
        if step in self.steps:
            self.steps[step].output = output
