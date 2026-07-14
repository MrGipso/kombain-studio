import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import tempfile
from typing import Annotated, Any, Literal

from pydantic import Field

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH_ENV = "PIPELINE_SETTINGS_PATH"


def _default_settings_path() -> Path:
    configured = os.environ.get(SETTINGS_PATH_ENV, "").strip()
    if not configured:
        return ROOT / "content_pipeline_settings.json"
    path = Path(configured).expanduser()
    return path if path.is_absolute() else (ROOT / path).resolve()


DEFAULT_SETTINGS_PATH = _default_settings_path()
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
    comfy_ace_workflow: str = ""
    image_model_preference: str = "Auto"
    whisper_cli: str = ""
    whisper_model: str = ""
    ffmpeg: str = ""
    whisper_language: str = "auto"  # "auto" = whisper detects the language per track
    output_base: str = ""
    lm_auto_unload: bool = True
    lm_unload_delay: str = "0"
    # Stock footage route (Pexels / Pixabay). Empty keys disable the route.
    pexels_api_key: str = ""
    pixabay_api_key: str = ""
    lm_vision_model: str = ""
    stock_score_threshold: float = 0.6  # "accept": good enough, take it and stop
    stock_floor: float = 0.4            # minimum acceptable score before skipping
    stock_retry_queries: int = 1        # rephrase the query and retry this many times
    stock_max_seconds: int = 0  # 0 = fill the timeline slot; >0 caps clip length
    stock_candidates: int = 20
    stock_uniquify: bool = True   # bake a subtle grade + crop into stock clips
    stock_mirror: bool = False    # also horizontally flip stock (stronger, riskier)
    # Reference-insert route: LLM names an entity, backend resolves it on
    # Wikipedia/arXiv, screenshots the page and slides it in over a blurred
    # backdrop. reference_density caps how often the planner uses it.
    reference_enable: bool = True
    reference_density: str = "normal"  # off | sparse | normal
    # Free/archival stock sources (Wikimedia Commons, NASA, Internet Archive).
    # No API keys needed; opt-in because quality/format varies more than Pexels.
    stock_free_sources: bool = False
    # SFX layer (Woosh, Sony AI SFX model served via its API server). When enabled
    # and the server is reachable, per-shot ambient sound is generated from each
    # card's sfx_query and mixed under the voice at sfx_volume_db.
    sfx_enabled: bool = False
    woosh_url: str = "http://127.0.0.1:8500"
    sfx_volume_db: float = -24.0
    # Final-mix loudness normalization (YouTube target −14 LUFS). Optional.
    export_loudnorm: bool = False
    # Normalize the music bed's loudness before mixing (ACE-Step output varies).
    music_normalize: bool = False
    # Unifying grade + film grain baked into every export segment so stock, Krea
    # stills and LTX clips read as one camera. grain 0 disables noise.
    export_unify_grade: bool = False
    export_grain: int = 6
    # Ken Burns variety: alternate pan/zoom directions with easing per shot
    # (deterministic by shot index). Off = legacy uniform center zoom-in.
    kenburns_variety: bool = True
    # Variable stock cut rhythm: cuts vary in length and snap to sentence
    # boundaries instead of a metronomic fixed grid.
    stock_variable_rhythm: bool = True
    # LTX draft mode: "off" = always the full two-pass workflow, "all" = always
    # the fast single-pass draft, "auto" = draft for everything except
    # priority=high hero shots. Draft is ~2-3x faster, slightly softer picture.
    ltx_draft_mode: str = "off"
    comfy_ltx_draft_workflow: str = ""
    # Hash-bound QA of generated media.  The model reports typed visual
    # observations; a deterministic policy chooses a recommendation.  Shadow
    # records evidence only, review additionally flags the card needs_review.
    # Neither mode ever triggers generation automatically.
    ai_frame_qa: bool = False
    ai_qa_mode: Literal["shadow", "review"] = "shadow"
    ai_qa_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.45
    # LTX render resolution: "1920x1088" (Full HD) or "1280x704" (~720p, faster).
    # Dimensions must be multiples of 32 (LTX latent grid); the export always
    # scales/crops every clip to the chosen export profile (1920x1080 by default),
    # so lower-resolution LTX renders can still ship in the requested frame.
    ltx_resolution: str = "1920x1088"

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
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, p)
    finally:
        temporary.unlink(missing_ok=True)


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
