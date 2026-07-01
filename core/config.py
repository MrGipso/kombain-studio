import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = ROOT / "content_pipeline_settings.json"
PRESETS_DIR = ROOT / "presets"


@dataclass
class Settings:
    lm_url: str = "http://127.0.0.1:1234/v1"
    lm_model: str = "local-model"
    lm_embed_model: str = "text-embedding-nomic-embed-text-v1.5"
    lm_context: str = "32768"
    comfy_url: str = "http://127.0.0.1:8188"
    comfy_krea_workflow: str = ""
    comfy_ideogram_workflow: str = ""
    comfy_ltx_workflow: str = ""
    image_model_preference: str = "Auto"
    whisper_cli: str = ""
    whisper_model: str = ""
    ffmpeg: str = ""
    whisper_language: str = "ru"
    output_base: str = ""
    lm_auto_unload: bool = True
    lm_unload_delay: str = "0"

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class PresetConfig:
    name: str
    path: Path
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def routes(self) -> list[dict]:
        return self.raw.get("routes", [])

    @property
    def prompts(self) -> dict:
        return self.raw.get("prompts", {})

    @property
    def heuristics(self) -> dict:
        return self.raw.get("heuristics", {})

    @property
    def remotion_templates(self) -> dict:
        return self.raw.get("remotion_templates", {})

    @property
    def comfy_workflows(self) -> dict:
        return self.raw.get("comfy_workflows", {})

    @property
    def pipeline_steps(self) -> dict:
        return self.raw.get("pipeline", {})


def load_settings(path: Path | None = None) -> Settings:
    p = path or DEFAULT_SETTINGS_PATH
    if p and p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return Settings.from_dict(data)
        except (json.JSONDecodeError, OSError):
            pass
    return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> None:
    p = path or DEFAULT_SETTINGS_PATH
    data = {f.name: getattr(settings, f.name) for f in settings.__dataclass_fields__.values()}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_presets() -> list[PresetConfig]:
    if not PRESETS_DIR.exists():
        return []
    presets: list[PresetConfig] = []
    for entry in sorted(PRESETS_DIR.iterdir()):
        if entry.is_dir():
            pipeline_file = entry / "pipeline.json"
            if pipeline_file.exists():
                try:
                    raw = json.loads(pipeline_file.read_text(encoding="utf-8"))
                    name = raw.get("info", {}).get("name", entry.name)
                except (json.JSONDecodeError, OSError):
                    name = entry.name
                    raw = {}
                presets.append(PresetConfig(name=name, path=entry, raw=raw))
    return presets


def load_preset(name: str) -> PresetConfig | None:
    for preset in list_presets():
        if preset.name == name or preset.path.name == name:
            return preset
    return None


def load_preset_by_path(path: Path) -> PresetConfig | None:
    pipeline_file = path / "pipeline.json"
    if pipeline_file.exists():
        try:
            raw = json.loads(pipeline_file.read_text(encoding="utf-8"))
            name = raw.get("info", {}).get("name", path.name)
            return PresetConfig(name=name, path=path, raw=raw)
        except (json.JSONDecodeError, OSError):
            pass
    return None
