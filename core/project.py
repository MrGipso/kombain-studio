import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import Settings
from core.locale import Locale, load_locale
from core.models import slugify


PROJECT_FILES = {
    "transcript": "01_transcript.txt",
    "transcript_segments": "01_transcript_segments.json",
    "whisper_raw": "01_whisper_raw.json",
    "scenes": "02_scenes.json",
    "edit_plan": "03_edit_plan.csv",
    "shot_list": "04_shot_list.json",
    "asset_requests": "04_asset_requests.csv",
    "subtitles_whisper": "04_subtitles_whisper.srt",
    "visual_plan": "05_visual_plan.md",
    "asset_board": "06_asset_board.json",
    "validation_report": "07_validation_report.json",
}


@dataclass
class Project:
    name: str
    dir: Path
    settings: Settings = field(default_factory=Settings)
    locale: Locale | None = None
    preset: str = "universal"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def voice_dir(self) -> Path:
        return self.dir / "voice"

    @property
    def assets_dir(self) -> Path:
        return self.dir / "assets"

    @property
    def assets_images_dir(self) -> Path:
        return self.assets_dir / "images"

    @property
    def assets_videos_dir(self) -> Path:
        return self.assets_dir / "videos"

    @property
    def analysis_dir(self) -> Path:
        return self.dir / "analysis"

    @property
    def notes_dir(self) -> Path:
        return self.dir / "notes"

    @property
    def renders_dir(self) -> Path:
        return self.dir / "renders"

    @property
    def remotion_jobs_dir(self) -> Path:
        return self.dir / "remotion_jobs"

    @property
    def exports_dir(self) -> Path:
        return self.dir / "exports"

    def file_path(self, key: str) -> Path:
        filename = PROJECT_FILES.get(key)
        if filename:
            return self.dir / filename
        return self.dir / key

    def read_json(self, key: str) -> list | dict | None:
        path = self.file_path(key)
        if path and path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def write_json(self, key: str, data: Any) -> None:
        path = self.file_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def write_text(self, key: str, text: str) -> None:
        path = self.file_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def save_meta(self) -> None:
        meta = {
            "name": self.name,
            "preset": self.preset,
            "language": self.settings.whisper_language,
            "video_style": self.metadata.get("video_style", "knitted"),
            "style_anchor": self.metadata.get("style_anchor", ""),
            "character_description": self.metadata.get("character_description", ""),
            "seg_min_seconds": self.metadata.get("seg_min_seconds", 20),
            "seg_max_seconds": self.metadata.get("seg_max_seconds", 90),
            "seg_sensitivity": self.metadata.get("seg_sensitivity", 0.6),
            "seg_target_scenes": self.metadata.get("seg_target_scenes", 0),
        }
        meta_path = self.dir / ".project.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_locale(self, lang: str | None = None) -> Locale:
        lang = lang or self.settings.whisper_language or "ru"
        presets_dir = Path(__file__).resolve().parent.parent / "presets"
        preset_path = presets_dir / self.preset
        if not preset_path.exists():
            preset_path = presets_dir / "documentary"
        self.locale = load_locale(preset_path, lang)
        return self.locale


def create_project(
    name: str,
    output_base: Path,
    settings: Settings | None = None,
    preset: str = "universal",
) -> Project:
    dir_name = slugify(name) or "untitled"
    project_dir = (output_base / dir_name).resolve()
    project_dir.mkdir(parents=True, exist_ok=True)

    (project_dir / "voice").mkdir(exist_ok=True)
    (project_dir / "analysis").mkdir(exist_ok=True)
    (project_dir / "assets" / "images").mkdir(parents=True, exist_ok=True)
    (project_dir / "assets" / "videos").mkdir(parents=True, exist_ok=True)
    (project_dir / "notes").mkdir(exist_ok=True)
    (project_dir / "exports").mkdir(exist_ok=True)
    (project_dir / "transcription").mkdir(exist_ok=True)
    (project_dir / "visual_plan").mkdir(exist_ok=True)

    project = Project(
        name=name,
        dir=project_dir,
        settings=settings or Settings(),
        preset=preset,
    )
    project.save_meta()
    return project


def open_project(path: Path, settings: Settings | None = None) -> Project | None:
    if not path.exists() or not path.is_dir():
        return None
    meta_file = path / ".project.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            # ensure new fields exist
            meta.setdefault("video_style", "knitted")
            meta.setdefault("style_anchor", "")
            meta.setdefault("character_description", "")
            meta.setdefault("seg_min_seconds", 20)
            meta.setdefault("seg_max_seconds", 90)
            meta.setdefault("seg_sensitivity", 0.6)
            meta.setdefault("seg_target_scenes", 0)
            return Project(
                name=meta.get("name", path.name),
                dir=path,
                settings=settings or Settings(),
                preset=meta.get("preset", "universal"),
                metadata=meta,
            )
        except (json.JSONDecodeError, OSError):
            pass
    return Project(
        name=path.name,
        dir=path,
        settings=settings or Settings(),
    )


def list_projects(output_base: Path) -> list[Project]:
    if not output_base.exists():
        return []
    projects: list[Project] = []
    for entry in sorted(output_base.iterdir()):
        if entry.is_dir():
            proj = open_project(entry)
            if proj:
                projects.append(proj)
    return projects


