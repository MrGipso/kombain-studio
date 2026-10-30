import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import Settings
from core.files import preserve_corrupt_copy, read_text_with_retry, replace_with_retry
from core.models import slugify


CURRENT_PROJECT_SCHEMA_VERSION = 1
CURRENT_MANIFEST_SCHEMA_VERSION = 1
PIPELINE_MANIFEST_FILE = ".pipeline_manifest.json"

# These names describe logical pipeline stages rather than individual files.
# A stage can produce more than one artifact (transcription is the clearest
# example), while callers still retain access to every legacy PROJECT_FILES key.
PIPELINE_STAGE_ORDER: tuple[str, ...] = (
    "script",
    "transcript",
    "scenes",
    "shots",
    "assets",
    "timeline",
    "validation",
    "export",
)
PIPELINE_STAGE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "script": (),
    # A transcript may come from recorded/uploaded audio without an authored
    # script.  Script is therefore an optional scene-analysis input, not a
    # mandatory ancestor of transcription.
    "transcript": (),
    "scenes": ("transcript",),
    "shots": ("scenes",),
    "assets": ("shots",),
    "timeline": ("assets",),
    "validation": ("timeline",),
    "export": ("validation",),
}
PIPELINE_STAGE_OPTIONAL_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "script": (),
    "transcript": (),
    "scenes": ("script",),
    "shots": (),
    "assets": (),
    "timeline": (),
    "validation": (),
    "export": (),
}

# Short aliases are intentionally exported: they make the contract convenient
# for API code and keep the longer names self-documenting inside this module.
STAGE_ORDER = PIPELINE_STAGE_ORDER
STAGE_DEPENDENCIES = PIPELINE_STAGE_DEPENDENCIES
STAGE_OPTIONAL_DEPENDENCIES = PIPELINE_STAGE_OPTIONAL_DEPENDENCIES
PROJECT_MANIFEST_FILE = PIPELINE_MANIFEST_FILE

_STAGE_ALIASES = {
    "transcription": "transcript",
    "transcript_segments": "transcript",
    "whisper_raw": "transcript",
    "scene": "scenes",
    "shot": "shots",
    "shot_list": "shots",
    "edit_plan": "shots",
    "asset": "assets",
    "asset_board": "assets",
    "visual_plan": "assets",
    "validation_report": "validation",
    "exports": "export",
}

_STAGE_ARTIFACT_PATTERNS: dict[str, tuple[str, ...]] = {
    "script": ("00_script.txt",),
    "transcript": (
        "01_transcript.txt",
        "01_transcript_segments.json",
        "01_whisper_raw.json",
    ),
    "scenes": ("02_scenes.json",),
    "shots": (
        "03_edit_plan.csv",
        "03_context_bible.json",
        "03_global_visual_plan.json",
        "04_shot_list.json",
        "04_asset_requests.csv",
    ),
    "assets": ("05_visual_plan.md", "06_asset_board.json"),
    "timeline": ("08_timeline.json",),
    "validation": ("07_validation_report.json",),
    "export": ("exports/*.mp4",),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _json_hash_value(value: Any) -> Any:
    """Convert common project values into deterministic JSON-compatible data."""
    if is_dataclass(value) and not isinstance(value, type):
        return _json_hash_value(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_hash_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_hash_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        converted = [_json_hash_value(item) for item in value]
        return sorted(converted, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    if hasattr(value, "model_dump"):
        return _json_hash_value(value.model_dump(mode="json"))
    return value


def content_hash(value: Any) -> str:
    """Return a deterministic SHA-256 digest for content, files, or JSON data.

    Strings are hashed as UTF-8 content (never interpreted as paths). ``Path``
    objects hash file bytes; directories hash all contained files in stable
    relative-path order. Dictionaries are canonicalised, so key order does not
    affect their digest.
    """
    digest = hashlib.sha256()
    if isinstance(value, Path):
        if value.is_file():
            with value.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        if value.is_dir():
            for child in sorted((p for p in value.rglob("*") if p.is_file()), key=lambda p: p.as_posix()):
                relative = child.relative_to(value).as_posix().encode("utf-8")
                digest.update(len(relative).to_bytes(8, "big"))
                digest.update(relative)
                with child.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            return digest.hexdigest()
        raise FileNotFoundError(value)
    if isinstance(value, str):
        payload = value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
    else:
        payload = json.dumps(
            _json_hash_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    digest.update(payload)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + os.replace so a crash or a parallel reader never
    sees a half-written JSON (plain write_text truncates first, then writes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        replace_with_retry(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

PROJECT_FILES = {
    "script": "00_script.txt",
    "transcript": "01_transcript.txt",
    "transcript_segments": "01_transcript_segments.json",
    "whisper_raw": "01_whisper_raw.json",
    "scenes": "02_scenes.json",
    "edit_plan": "03_edit_plan.csv",
    "context_bible": "03_context_bible.json",
    "shot_list": "04_shot_list.json",
    "asset_requests": "04_asset_requests.csv",
    "subtitles_whisper": "04_subtitles_whisper.srt",
    "visual_plan": "05_visual_plan.md",
    "asset_board": "06_asset_board.json",
    "validation_report": "07_validation_report.json",
    "timeline": "08_timeline.json",
}


def canonical_stage(stage: str) -> str:
    """Return a canonical stage name, accepting legacy artifact-key aliases."""
    normalised = str(stage or "").strip().lower().replace("-", "_")
    normalised = _STAGE_ALIASES.get(normalised, normalised)
    if normalised not in PIPELINE_STAGE_DEPENDENCIES:
        raise ValueError(f"Unknown pipeline stage: {stage!r}")
    return normalised


def _blank_stage_state(status: str = "pending") -> dict[str, Any]:
    return {
        "status": status,
        "hash": None,
        "input_hash": None,
        "revision": 0,
        "created_at": None,
        "updated_at": None,
        "completed_at": None,
        "failed_at": None,
        "invalidated_at": None,
        "invalidated_by": None,
        "error": None,
    }


def _normalise_stage_state(value: Any) -> dict[str, Any]:
    state = _blank_stage_state()
    if isinstance(value, dict):
        state.update(value)
    if state.get("status") not in {"pending", "completed", "failed", "stale"}:
        state["status"] = "pending"
    try:
        state["revision"] = max(0, int(state.get("revision", 0)))
    except (TypeError, ValueError):
        state["revision"] = 0
    return state


def _hash_artifact_paths(paths: list[Path], root: Path) -> str | None:
    if not paths:
        return None
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return None
    return digest.hexdigest()


@dataclass
class Project:
    name: str
    dir: Path
    settings: Settings = field(default_factory=Settings)
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

    @property
    def manifest_path(self) -> Path:
        return self.dir / PIPELINE_MANIFEST_FILE

    @property
    def schema_version(self) -> int:
        """Schema read from .project.json (0 identifies a legacy project)."""
        try:
            return max(0, int(self.metadata.get("schema_version", 0)))
        except (TypeError, ValueError):
            return 0

    def file_path(self, key: str) -> Path:
        filename = PROJECT_FILES.get(key)
        if filename:
            return self.dir / filename
        return self.dir / key

    def read_json(self, key: str) -> list | dict | None:
        """Return a JSON artifact, or None when it is absent or unusable.

        Corrupt content is preserved under ``.corrupt/`` first, because callers
        treat None as "empty" and may write a fresh artifact over the file.
        """
        path = self.file_path(key)
        if path and path.exists():
            try:
                return json.loads(read_text_with_retry(path))
            except json.JSONDecodeError:
                preserve_corrupt_copy(path)
                return None
            except OSError:
                return None
        return None

    def write_json(self, key: str, data: Any) -> None:
        path = self.file_path(key)
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))

    def write_text(self, key: str, text: str) -> None:
        path = self.file_path(key)
        atomic_write_text(path, text)

    def effective_settings(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return runtime settings overlaid with project-scoped pipeline choices.

        The returned dictionary is detached from both ``Settings`` and project
        metadata, so adapters can add request-specific overrides without
        mutating either source.
        """
        values = asdict(self.settings)
        explicit_project_keys = {
            "video_style",
            "style_anchor",
            "character_description",
            "shot_mode",
            "route_priority",
            "stock_mode",
            "stock_cut_seconds",
            "detected_language",
        }
        for key, value in self.metadata.items():
            if key in explicit_project_keys or key.startswith("seg_"):
                values[key] = value
        if overrides:
            values.update(dict(overrides))
        return values

    def _artifact_paths(self, stage: str) -> list[Path]:
        canonical = canonical_stage(stage)
        paths: list[Path] = []
        for pattern in _STAGE_ARTIFACT_PATTERNS[canonical]:
            if any(token in pattern for token in "*?["):
                paths.extend(path for path in self.dir.glob(pattern) if path.is_file())
            else:
                path = self.dir / pattern
                if path.is_file():
                    paths.append(path)
        # A pattern cannot currently overlap another pattern, but deduplicating
        # makes that invariant explicit if artifact layouts expand later.
        return sorted(set(paths), key=lambda item: item.relative_to(self.dir).as_posix())

    def derive_stage_state(self, stage: str) -> dict[str, Any]:
        """Derive state from legacy artifacts without creating a manifest."""
        canonical = canonical_stage(stage)
        paths = self._artifact_paths(canonical)
        if not paths:
            state = _blank_stage_state()
            state["source"] = "legacy"
            return state
        try:
            timestamp = _utc_from_timestamp(max(path.stat().st_mtime for path in paths))
        except OSError:
            timestamp = None
        state = _blank_stage_state("completed")
        state.update(
            {
                "hash": _hash_artifact_paths(paths, self.dir),
                "created_at": timestamp,
                "updated_at": timestamp,
                "completed_at": timestamp,
                "source": "legacy",
            }
        )
        return state

    def derive_stage_states(self) -> dict[str, dict[str, Any]]:
        return {stage: self.derive_stage_state(stage) for stage in PIPELINE_STAGE_ORDER}

    def load_manifest(self) -> dict[str, Any]:
        """Load the manifest, filling absent stages from legacy project files.

        Missing or malformed manifests never cause legacy artifacts to be
        rewritten. The derived in-memory representation is persisted only when
        a caller explicitly changes pipeline state.
        """
        raw: dict[str, Any] = {}
        manifest_exists = self.manifest_path.is_file()
        if manifest_exists:
            try:
                loaded = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded
            except (json.JSONDecodeError, OSError):
                raw = {}

        manifest = dict(raw)
        schema_version = manifest.get("schema_version", CURRENT_MANIFEST_SCHEMA_VERSION)
        try:
            manifest["schema_version"] = max(0, int(schema_version))
        except (TypeError, ValueError):
            manifest["schema_version"] = CURRENT_MANIFEST_SCHEMA_VERSION
        try:
            manifest["revision"] = max(0, int(manifest.get("revision", 0)))
        except (TypeError, ValueError):
            manifest["revision"] = 0
        manifest.setdefault("created_at", None)
        manifest.setdefault("updated_at", None)
        manifest["stage_order"] = list(PIPELINE_STAGE_ORDER)
        manifest["stage_dependencies"] = {
            stage: list(dependencies)
            for stage, dependencies in PIPELINE_STAGE_DEPENDENCIES.items()
        }
        manifest["stage_optional_dependencies"] = {
            stage: list(dependencies)
            for stage, dependencies in PIPELINE_STAGE_OPTIONAL_DEPENDENCIES.items()
        }

        raw_stages = raw.get("stages") if isinstance(raw.get("stages"), dict) else {}
        stages: dict[str, Any] = {
            name: value for name, value in raw_stages.items() if name not in PIPELINE_STAGE_ORDER
        }
        for stage in PIPELINE_STAGE_ORDER:
            if isinstance(raw_stages.get(stage), dict):
                state = _normalise_stage_state(raw_stages[stage])
                state.setdefault("source", "manifest")
            else:
                state = self.derive_stage_state(stage)
            stages[stage] = state
        manifest["stages"] = stages
        manifest["derived"] = not manifest_exists or not raw
        return manifest

    def save_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Atomically persist an artifact manifest and return stored data."""
        stored = dict(manifest)
        stored.pop("derived", None)
        try:
            loaded_schema_version = int(stored.get("schema_version", 0))
        except (TypeError, ValueError):
            loaded_schema_version = 0
        # Never silently downgrade a manifest written by a newer application.
        stored["schema_version"] = max(CURRENT_MANIFEST_SCHEMA_VERSION, loaded_schema_version)
        stored.setdefault("revision", 0)
        if not stored.get("created_at"):
            stored["created_at"] = _utc_now()
        stored["updated_at"] = _utc_now()
        atomic_write_text(
            self.manifest_path,
            json.dumps(stored, ensure_ascii=False, indent=2, sort_keys=True),
        )
        return stored

    def stage_state(self, stage: str) -> dict[str, Any]:
        canonical = canonical_stage(stage)
        return dict(self.load_manifest()["stages"][canonical])

    # An explicit getter reads naturally in routers and keeps stage_state terse
    # for engine code.
    get_stage_state = stage_state

    def stage_states(self) -> dict[str, dict[str, Any]]:
        manifest = self.load_manifest()
        return {stage: dict(manifest["stages"][stage]) for stage in PIPELINE_STAGE_ORDER}

    pipeline_state = stage_states

    def stage_dependencies_current(self, stage: str, *, recursive: bool = True) -> bool:
        """Return whether the required inputs of *stage* are current.

        Optional inputs deliberately do not block a stage that does not use
        them.  When an optional input exists and changes, its optional DAG edge
        still invalidates consumers through :meth:`_downstream_stages`.
        Recursive checking is the safe default for gates: a completed timeline
        cannot conceal a stale asset or transcript ancestor.
        """
        canonical = canonical_stage(stage)
        states = self.stage_states()
        required: set[str] = set(PIPELINE_STAGE_DEPENDENCIES[canonical])
        if recursive:
            pending = list(required)
            while pending:
                dependency = pending.pop()
                for ancestor in PIPELINE_STAGE_DEPENDENCIES[dependency]:
                    if ancestor not in required:
                        required.add(ancestor)
                        pending.append(ancestor)
        return all(
            states[dependency]["status"] == "completed"
            for dependency in required
        )

    @staticmethod
    def content_hash(value: Any) -> str:
        return content_hash(value)

    @staticmethod
    def _downstream_stages(stage: str) -> list[str]:
        canonical = canonical_stage(stage)
        downstream: set[str] = set()
        changed = True
        while changed:
            changed = False
            for candidate, dependencies in PIPELINE_STAGE_DEPENDENCIES.items():
                if candidate in downstream or candidate == canonical:
                    continue
                all_dependencies = dependencies + PIPELINE_STAGE_OPTIONAL_DEPENDENCIES.get(
                    candidate, ()
                )
                if canonical in all_dependencies or any(
                    dependency in downstream for dependency in all_dependencies
                ):
                    downstream.add(candidate)
                    changed = True
        return [name for name in PIPELINE_STAGE_ORDER if name in downstream]

    @staticmethod
    def _invalidate_manifest_stages(
        manifest: dict[str, Any],
        stage: str,
        timestamp: str,
        reason: str | None = None,
        *,
        include_stage: bool = False,
    ) -> list[str]:
        canonical = canonical_stage(stage)
        invalidated: list[str] = []
        stages = manifest["stages"]
        targets = Project._downstream_stages(canonical)
        if include_stage:
            targets = [canonical, *targets]
        for target in targets:
            state = _normalise_stage_state(stages.get(target))
            # A stage that has never produced anything is pending, not stale.
            if state["status"] == "pending" and not state.get("hash"):
                continue
            state.update(
                {
                    "status": "stale",
                    "revision": state["revision"] + 1,
                    "updated_at": timestamp,
                    "invalidated_at": timestamp,
                    "invalidated_by": canonical,
                    "failed_at": None,
                    "error": reason,
                    "source": "manifest",
                }
            )
            stages[target] = state
            invalidated.append(target)
        return invalidated

    def invalidate_stage(
        self,
        stage: str,
        reason: str | None = None,
        *,
        include_stage: bool = True,
    ) -> list[str]:
        """Mark a stage and its existing downstream outputs stale.

        ``include_stage=False`` preserves the historical downstream-only
        operation used when a stage has just been rebuilt.  Keeping both paths
        in this method prevents routers from implementing subtly different
        invalidation semantics.
        """
        canonical = canonical_stage(stage)
        manifest = self.load_manifest()
        timestamp = _utc_now()
        invalidated = self._invalidate_manifest_stages(
            manifest,
            canonical,
            timestamp,
            reason,
            include_stage=include_stage,
        )
        if invalidated:
            manifest["revision"] = int(manifest.get("revision", 0)) + 1
            self.save_manifest(manifest)
        return invalidated

    def invalidate_downstream(self, stage: str, reason: str | None = None) -> list[str]:
        """Compatibility wrapper for the historical downstream-only action."""
        return self.invalidate_stage(stage, reason, include_stage=False)

    def mark_stage_completed(
        self,
        stage: str,
        content: Any = None,
        *,
        content_hash: str | None = None,
        input_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a successful stage and invalidate outputs if its hash changed."""
        canonical = canonical_stage(stage)
        manifest = self.load_manifest()
        previous = _normalise_stage_state(manifest["stages"].get(canonical))
        if content_hash is None:
            if content is not None:
                digest = globals()["content_hash"](content)
            else:
                digest = _hash_artifact_paths(self._artifact_paths(canonical), self.dir)
        else:
            digest = str(content_hash)

        timestamp = _utc_now()
        # First producing an optional input (most notably an authored script)
        # also changes already-built consumers: scenes created before the
        # script existed must be rebuilt to use it.
        changed = previous.get("hash") != digest
        if changed:
            self._invalidate_manifest_stages(
                manifest,
                canonical,
                timestamp,
                reason=f"upstream stage {canonical} changed",
            )

        state = previous
        state.update(
            {
                "status": "completed",
                "hash": digest,
                "input_hash": input_hash,
                "revision": state["revision"] + 1,
                "created_at": state.get("created_at") or timestamp,
                "updated_at": timestamp,
                "completed_at": timestamp,
                "failed_at": None,
                "invalidated_at": None,
                "invalidated_by": None,
                "error": None,
                "source": "manifest",
            }
        )
        if metadata is not None:
            state["metadata"] = dict(metadata)
        manifest["stages"][canonical] = state
        manifest["revision"] = int(manifest.get("revision", 0)) + 1
        if not manifest.get("created_at"):
            manifest["created_at"] = timestamp
        self.save_manifest(manifest)
        return dict(state)

    def mark_stage_failed(
        self,
        stage: str,
        error: str,
        *,
        input_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a failed attempt without deleting a prior stage artifact."""
        canonical = canonical_stage(stage)
        manifest = self.load_manifest()
        state = _normalise_stage_state(manifest["stages"].get(canonical))
        timestamp = _utc_now()
        state.update(
            {
                "status": "failed",
                "input_hash": input_hash,
                "revision": state["revision"] + 1,
                "created_at": state.get("created_at") or timestamp,
                "updated_at": timestamp,
                "failed_at": timestamp,
                "invalidated_at": None,
                "invalidated_by": None,
                "error": str(error),
                "source": "manifest",
            }
        )
        if metadata is not None:
            state["metadata"] = dict(metadata)
        manifest["stages"][canonical] = state
        manifest["revision"] = int(manifest.get("revision", 0)) + 1
        if not manifest.get("created_at"):
            manifest["created_at"] = timestamp
        self.save_manifest(manifest)
        return dict(state)

    def save_meta(self) -> None:
        # Preserve all metadata loaded from a newer/extended project file. This
        # makes an explicit save a non-destructive schema upgrade.
        meta = dict(self.metadata)
        meta.update(
            {
                "schema_version": max(CURRENT_PROJECT_SCHEMA_VERSION, self.schema_version),
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
                # Background music (ACE-Step). Project-level audio, mixed under the voice.
                "music_tags": self.metadata.get("music_tags", ""),
                "music_bpm": self.metadata.get("music_bpm", 90),
                "music_duration": self.metadata.get("music_duration", 90),
                "music_enabled": self.metadata.get("music_enabled", True),
                "music_volume_db": self.metadata.get("music_volume_db", -18),
                "music_duck": self.metadata.get("music_duck", True),
                "music_selected": self.metadata.get("music_selected", ""),
                # Shot mode: "ai" (all AI), "stock" (all stock), "hybrid" (LLM picks
                # stock / Ken Burns / AI per shot). stock_cut_seconds caps stock cuts.
                "shot_mode": self.metadata.get(
                    "shot_mode", "stock" if self.metadata.get("stock_mode") else "ai"
                ),
                "route_priority": self.metadata.get("route_priority", "balanced"),
                "stock_mode": self.metadata.get("stock_mode", False),
                "stock_cut_seconds": self.metadata.get("stock_cut_seconds", 5),
                # Language whisper auto-detected for this project's audio.
                "detected_language": self.metadata.get("detected_language", ""),
            }
        )
        meta_path = self.dir / ".project.json"
        atomic_write_text(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
        self.metadata.clear()
        self.metadata.update(meta)


class ProjectExists(Exception):
    """A project directory with this slug already exists.

    Creation must fail instead of reusing or silently renaming the directory:
    callers can then present an explicit conflict and let the user choose a
    different project name.
    """


def create_project(
    name: str,
    output_base: Path,
    settings: Settings | None = None,
    preset: str = "universal",
) -> Project:
    dir_name = slugify(name) or "untitled"
    project_dir = (output_base / dir_name).resolve()
    # Do not silently reuse *or rename* an existing directory. Both behaviours
    # hide a slug collision from the caller and can make a project appear under
    # a different id than requested.
    if project_dir.exists():
        raise ProjectExists(f"Проект с идентификатором «{dir_name}» уже существует")
    try:
        # exist_ok=False also closes the race between the check above and mkdir.
        project_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ProjectExists(f"Проект с идентификатором «{dir_name}» уже существует") from exc

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


def safe_project_path(project_id: str, output_base: Path | str) -> Path:
    """Resolve a client-supplied project id to a directory strictly inside
    output_base.

    Rejects absolute paths, drive-qualified paths and parent-directory
    traversal ("..") so a request cannot escape the projects root and read or
    write arbitrary locations on disk. Raises ValueError on any invalid id.
    """
    raw = (project_id or "").strip()
    # Reject Windows drive/backslash forms explicitly so the guard behaves the
    # same on any OS (on Linux "C:\..." would otherwise look like a plain name).
    if not raw or "\\" in raw or (len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha()):
        raise ValueError("Invalid project id")
    p = Path(raw)
    if p.is_absolute() or p.drive or p.anchor or ".." in p.parts:
        raise ValueError("Invalid project id")
    base = Path(output_base or "projects").resolve()
    candidate = (base / raw).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError("Invalid project id")
    return candidate


def is_within(child: Path | str, root: Path | str) -> bool:
    """True if `child` resolves to a location inside (or equal to) `root`."""
    try:
        child_r = Path(child).resolve()
        root_r = Path(root).resolve()
    except (OSError, ValueError):
        return False
    return child_r == root_r or root_r in child_r.parents


def open_project(path: Path, settings: Settings | None = None) -> Project | None:
    if not path.exists() or not path.is_dir():
        return None
    meta_file = path / ".project.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                raise ValueError("Project metadata must be a JSON object")
            # Missing means the pre-manifest legacy schema. Opening is read-only;
            # the file is upgraded only on an explicit save_meta() call.
            meta.setdefault("schema_version", 0)
            # ensure new fields exist
            meta.setdefault("video_style", "knitted")
            meta.setdefault("style_anchor", "")
            meta.setdefault("character_description", "")
            meta.setdefault("seg_min_seconds", 20)
            meta.setdefault("seg_max_seconds", 90)
            meta.setdefault("seg_sensitivity", 0.6)
            meta.setdefault("seg_target_scenes", 0)
            meta.setdefault("music_tags", "")
            meta.setdefault("music_bpm", 90)
            meta.setdefault("music_duration", 90)
            meta.setdefault("music_enabled", True)
            meta.setdefault("music_volume_db", -18)
            meta.setdefault("music_duck", True)
            meta.setdefault("music_selected", "")
            meta.setdefault("stock_mode", False)
            meta.setdefault("shot_mode", "stock" if meta.get("stock_mode") else "ai")
            meta.setdefault("route_priority", "balanced")
            meta.setdefault("stock_cut_seconds", 5)
            meta.setdefault("detected_language", "")
            return Project(
                name=meta.get("name", path.name),
                dir=path,
                settings=settings or Settings(),
                preset=meta.get("preset", "universal"),
                metadata=meta,
            )
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    return Project(
        name=path.name,
        dir=path,
        settings=settings or Settings(),
    )


def is_project_dir(path: Path) -> bool:
    """Return whether *path* has an explicit, readable project manifest.

    A manifest without ``schema_version`` remains valid for compatibility with
    projects created before versioned manifests. Merely being a directory is
    intentionally insufficient: cache/tooling folders must not surface in the
    project picker.
    """
    if not path.is_dir():
        return False
    manifest = path / ".project.json"
    if not manifest.is_file():
        return False
    try:
        return isinstance(json.loads(manifest.read_text(encoding="utf-8")), dict)
    except (json.JSONDecodeError, OSError, ValueError):
        return False


def list_projects(output_base: Path) -> list[Project]:
    if not output_base.exists():
        return []
    projects: list[Project] = []
    for entry in sorted(output_base.iterdir()):
        # Skip dotfolders and any directory without a project manifest.
        if entry.name.startswith(".") or not is_project_dir(entry):
            continue
        proj = open_project(entry)
        if proj:
            projects.append(proj)
    return projects
