import json
import shutil
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Signal

from core import board as board_store
from core import comfy_generation
from core import lm as lm_client
from core import scenes as scene_tools
from core import shot_planning as shot_planner
from core import transcribe as transcription
from core.models import Scene, Shot, AssetCard, TranscriptSegment as CoreTranscriptSegment
from core.project import Project


def lm_unload_delay_seconds(settings: dict) -> int | None:
    if not settings.get("lm_auto_unload", True):
        return None
    try:
        return max(0, int(str(settings.get("lm_unload_delay", "0")).strip() or "0"))
    except ValueError:
        return 60


def lm_ttl_seconds(settings: dict) -> int | None:
    delay = lm_unload_delay_seconds(settings)
    return delay if delay and delay > 0 else None


def unload_lm_if_immediate(settings: dict, reason: str, log: Callable[[str], None]):
    delay = lm_unload_delay_seconds(settings)
    if delay is None:
        log("Авто-выгрузка LM Studio выключена")
        return
    if delay > 0:
        log(f"LM Studio TTL установлен: {delay} сек.")
        return
    base_url = settings.get("lm_url", "http://127.0.0.1:1234/v1")
    model = str(settings.get("lm_model", "local-model")).strip()
    if not model:
        log("LM Studio unload skipped: model id пустой")
        return
    try:
        log(f"LM Studio unload: model={model}, reason={reason}")
        result = lm_client.unload_model(base_url, model)
        log(f"LM Studio unload result: {json.dumps(result, ensure_ascii=False)}")
    except Exception as exc:
        log(f"LM Studio unload error: {lm_client.explain_error(exc)}")


class PipelineWorker(QThread):
    log = Signal(str)
    progress = Signal(float)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, target: Callable, *args, **kwargs):
        super().__init__()
        self.target = target
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            self.kwargs["log_callback"] = self._log
            result = self.target(*self.args, **self.kwargs)
            self.done.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _log(self, message: str):
        self.log.emit(message)


class TranscriberWorker(QThread):
    log = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, project: Project, audio_path: Path, settings: dict):
        super().__init__()
        self.project = project
        self.audio_path = audio_path
        self.settings = settings

    def run(self):
        try:
            whisper_cli = Path(self.settings.get("whisper_cli", ""))
            model_path = Path(self.settings.get("whisper_model", ""))
            ffmpeg_path = Path(self.settings.get("ffmpeg", ""))
            language = self.settings.get("whisper_language", "ru")

            if not whisper_cli.exists():
                whisper_cli = transcription.find_default_tool("whisper-cli.exe") or whisper_cli
            if not model_path.exists():
                discovered = transcription.find_default_model()
                model_path = Path(discovered) if discovered else model_path
            if not ffmpeg_path.exists():
                ffmpeg_path = transcription.find_default_tool("ffmpeg.exe") or ffmpeg_path

            transcript_text, segments, json_path, srt_path = transcription.transcribe_with_whisper_cpp(
                audio_path=self.audio_path,
                whisper_cli=Path(whisper_cli) if isinstance(whisper_cli, str) else whisper_cli,
                model_path=Path(model_path) if isinstance(model_path, str) else model_path,
                ffmpeg_path=Path(ffmpeg_path) if isinstance(ffmpeg_path, str) else ffmpeg_path,
                language=language,
                output_dir=self.project.dir / "transcription",
                log_callback=self._log,
            )

            # Convert to core models
            core_segments = [
                CoreTranscriptSegment(s.start, s.end, s.text)
                for s in segments
            ]

            result = {
                "text": transcript_text,
                "segments": core_segments,
                "json_path": json_path,
                "srt_path": srt_path,
            }

            # Write project files
            self.project.voice_dir.mkdir(parents=True, exist_ok=True)
            voice_target = self.project.voice_dir / self.audio_path.name
            if self.audio_path.exists() and self.audio_path.resolve() != voice_target.resolve():
                shutil.copy2(self.audio_path, voice_target)
                self.log.emit(f"Голосовая дорожка сохранена: {voice_target}")
            self.project.write_text("transcript", transcript_text)
            core_seg_data = [{"start": s.start, "end": s.end, "text": s.text} for s in core_segments]
            self.project.write_json("transcript_segments", core_seg_data)
            if json_path:
                self.project.write_json("whisper_raw", json.loads(json_path.read_text("utf-8")))

            self.done.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _log(self, message: str):
        self.log.emit(message)


class SceneAnalyzerWorker(QThread):
    log = Signal(str)
    scene_progress = Signal(object)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, project: Project, segments: list, settings: dict):
        super().__init__()
        self.project = project
        self.segments = segments
        self.settings = settings

    def run(self):
        try:
            base_url = self.settings.get("lm_url", "http://127.0.0.1:1234/v1")
            model = self.settings.get("lm_model", "local-model")
            try:
                context_length = int(str(self.settings.get("lm_context", "32768")).strip() or "32768")
            except ValueError:
                context_length = 32768
            ttl_seconds = lm_ttl_seconds(self.settings)

            t_segments = [
                CoreTranscriptSegment(s.start, s.end, s.text)
                for s in self.segments
            ]
            try:
                self.log.emit(f"Загружаю LM Studio model с context_length={context_length}")
                load_result = lm_client.load_model(base_url, model, context_length)
                load_config = load_result.get("load_config", {}) if isinstance(load_result, dict) else {}
                active_context = load_config.get("context_length", context_length)
                self.log.emit(f"LM Studio active context_length={active_context}")

                def _on_scene(scene):
                    core = Scene(**scene.__dict__)
                    self.scene_progress.emit(core)

                # The canonical core call keeps parsing/timeline behaviour the
                # same for web and desktop. Emit the completed scenes through
                # the existing progress signal once the LM response is valid.
                scenes = scene_tools.call_lm_studio_for_segments(
                    base_url, model, t_segments,
                    ttl_seconds=ttl_seconds,
                )
                for scene in scenes:
                    _on_scene(scene)
            except Exception as exc:
                self.log.emit(f"LM Studio не смог проанализировать сцены: {exc}")
                self.log.emit("Собираю сцены локально по таймкодам транскрипции")
                scenes = scene_tools.build_scenes_from_segments(t_segments)

            core_scenes = [Scene(**s.__dict__) for s in scenes]
            scenes_data = [s.__dict__ for s in core_scenes]
            self.project.write_json("scenes", scenes_data)
            unload_lm_if_immediate(self.settings, "scene analysis finished", self.log.emit)
            self.done.emit(core_scenes)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _log(self, message: str):
        self.log.emit(message)


class ShotListWorker(QThread):
    log = Signal(str)
    shot_progress = Signal(object)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, project: Project, scenes: list, settings: dict):
        super().__init__()
        self.project = project
        self.scenes = scenes
        self.settings = settings

    def run(self):
        try:
            pipeline_scenes = [Scene(**s.__dict__) for s in self.scenes]
            image_pref = self.settings.get("image_model_preference", "Auto")
            base_url = self.settings.get("lm_url", "http://127.0.0.1:1234/v1")
            model = self.settings.get("lm_model", "local-model")
            seg_data = self.project.read_json("transcript_segments") or []
            pipeline_segments = [CoreTranscriptSegment(**s) for s in seg_data]
            style_anchor = self.settings.get("style_anchor") or self.project.metadata.get("style_anchor", "")
            character_description = self.settings.get("character_description") or self.project.metadata.get("character_description", "")
            video_style = self.settings.get("video_style") or self.project.metadata.get("video_style", "knitted")

            def _on_scene_shots(raw_shots):
                core = [Shot(**s.__dict__) for s in raw_shots]
                self.shot_progress.emit(core)

            ttl_seconds = lm_ttl_seconds(self.settings)
            reference_density = (
                str(self.settings.get("reference_density", "normal") or "normal")
                if self.settings.get("reference_enable", True)
                else "off"
            )
            shots = shot_planner.call_lm_studio_for_shots(
                base_url, model, pipeline_scenes, image_pref,
                segments=pipeline_segments, on_scene_shots=_on_scene_shots,
                ttl_seconds=ttl_seconds,
                style_anchor=style_anchor,
                character_description=character_description,
                video_style=video_style,
                reference_density=reference_density,
            )

            core_shots = [Shot(**s.__dict__) for s in shots]
            shots_data = [s.__dict__ for s in core_shots]
            self.project.write_json("shot_list", shots_data)

            cards = board_store.build_asset_board(self.project.dir)
            board_store.write_asset_board(self.project.dir, cards)
            core_cards = [AssetCard(**c.__dict__) for c in cards]
            unload_lm_if_immediate(self.settings, "shot list and asset board finished", self.log.emit)
            self.done.emit({"shots": core_shots, "cards": core_cards})
        except Exception as exc:
            self.failed.emit(str(exc))

    def _log(self, message: str):
        self.log.emit(message)


class AssetGeneratorWorker(QThread):
    log = Signal(str)
    progress = Signal(float)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, project: Project, card_index: int, mode: str, comfy_url: str):
        super().__init__()
        self.project = project
        self.card_index = card_index
        self.mode = mode
        self.comfy_url = comfy_url

    def run(self):
        try:
            cards = board_store.load_asset_board(self.project.dir)
            if self.card_index >= len(cards):
                raise RuntimeError(f"Card index {self.card_index} out of range")
            card = cards[self.card_index]
            card.status = "generating"
            board_store.write_asset_board(self.project.dir, cards)
            self.log.emit(f"Генерация кадра {card.shot_index:03d} via {self.mode}")

            if self.mode in {"route", "graphic"}:
                card.route = "image+video"
                card.visual_type = "ai_image_then_i2v"
                card.remotion_template = ""
                card.remotion_output = ""
                if not card.image_model:
                    card.image_model = board_store.preferred_realistic_image_model(
                        self.project.settings.image_model_preference
                    )
                if not card.video_model:
                    card.video_model = "ltx2.3_i2v"
                generated_any = False
                if card.image_model:
                    image_path = comfy_generation.generate_comfy_image(
                        self.comfy_url,
                        self.project.dir,
                        card,
                        settings=self.project.settings,
                    )
                    card.image_output = str(image_path)
                    generated_any = True
                if card.video_model:
                    video_path = comfy_generation.generate_comfy_ltx_video(
                        self.comfy_url,
                        self.project.dir,
                        card,
                        settings=self.project.settings,
                    )
                    card.video_output = str(video_path)
                    generated_any = True
                if not generated_any:
                    raise RuntimeError("This card needs an image model and video model for AI video generation.")
            elif self.mode == "image":
                image_path = comfy_generation.generate_comfy_image(
                    self.comfy_url,
                    self.project.dir,
                    card,
                    settings=self.project.settings,
                )
                card.image_output = str(image_path)
                card.status = "image_ready"
                card.approved = False
                board_store.write_asset_board(self.project.dir, cards)
                self.log.emit(f"Кадр {card.shot_index:03d} image ready")
                self.done.emit(cards)
                return
            elif self.mode == "video":
                video_path = comfy_generation.generate_comfy_ltx_video(
                    self.comfy_url,
                    self.project.dir,
                    card,
                    settings=self.project.settings,
                )
                card.video_output = str(video_path)
            else:
                raise RuntimeError(f"Unknown mode: {self.mode}")

            card.status = "generated"
            card.approved = False
            board_store.write_asset_board(self.project.dir, cards)
            self.log.emit(f"Кадр {card.shot_index:03d} generated")
            self.done.emit(cards)
        except Exception as exc:
            try:
                cards = board_store.load_asset_board(self.project.dir)
                if 0 <= self.card_index < len(cards):
                    card = cards[self.card_index]
                    card.status = "needs_review"
                    card.notes = (card.notes + "\n" if card.notes else "") + f"Generation error: {exc}"
                    board_store.write_asset_board(self.project.dir, cards)
            except Exception:
                pass
            self.failed.emit(str(exc))

    def _render_remotion(self, card, cards):
        raise RuntimeError("Remotion rendering was removed; use AI video generation instead.")

    def _render_motion_text(self, card, cards):
        raise RuntimeError("Remotion motion-text rendering was removed; use AI video generation instead.")

    def _log(self, message: str):
        self.log.emit(message)




