from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from core import board as board_store
from core import lm as lm_client
from core import scenes as scene_tools
from core import shot_planning as shot_planner
from core.models import AssetCard, Scene, Shot, TranscriptSegment
from core.project import Project
from core.validation import write_validation_report


def validation_summary_message(report: dict) -> str:
    summary = report.get("summary", {})
    counts = summary.get("counts", {})
    return (
        f"Validation {summary.get('status', 'unknown')}: "
        f"errors={counts.get('error', 0)}, "
        f"warnings={counts.get('warning', 0)}"
    )


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


class _SceneAnalysisWorker(QThread):
    log = Signal(str)
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, project: Project, segments: list[TranscriptSegment], settings: dict):
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

            style_anchor = self.settings.get("style_anchor") or self.project.metadata.get("style_anchor", "")
            character_description = self.settings.get("character_description") or self.project.metadata.get("character_description", "")
            video_style = self.settings.get("video_style") or self.project.metadata.get("video_style", "knitted")

            t_segments = [
                TranscriptSegment(s.start, s.end, s.text)
                for s in self.segments
            ]
            embed_model = self.settings.get("lm_embed_model", "text-embedding-nomic-embed-text-v1.5")
            seg_min = int(self.project.metadata.get("seg_min_seconds", 20) or 20)
            seg_max = int(self.project.metadata.get("seg_max_seconds", 90) or 90)
            seg_sensitivity = float(self.project.metadata.get("seg_sensitivity", 0.6) or 0.6)
            seg_target = int(self.project.metadata.get("seg_target_scenes", 0) or 0)
            try:
                # PRIMARY: deterministic boundaries via embeddings; LLM only labels.
                self.log.emit("Сегментация сцен по эмбеддингам...")
                scenes = scene_tools.build_scenes_by_embedding(
                    t_segments, base_url, embed_model, fallback=False, ttl_seconds=ttl_seconds,
                    min_seconds=seg_min, max_seconds=seg_max, sensitivity=seg_sensitivity, target_scenes=seg_target,
                )
                # Free the embedding model from VRAM (same auto-unload policy) before
                # loading the chat model for labelling.
                if lm_unload_delay_seconds(self.settings) == 0 and embed_model:
                    try:
                        lm_client.unload_model(base_url, embed_model)
                        self.log.emit(f"Эмбеддинг-модель выгружена: {embed_model}")
                    except Exception as exc:
                        self.log.emit(f"Выгрузка эмбеддинг-модели: {lm_client.explain_error(exc)}")
                self.log.emit(f"Границы по эмбеддингам: {len(scenes)} сцен; озаглавливаю через LLM...")
                try:
                    lm_client.load_model(base_url, model, context_length)
                    scenes = scene_tools.enrich_scenes_with_lm(
                        scenes, base_url, model, ttl_seconds=ttl_seconds,
                        style_anchor=style_anchor,
                        character_description=character_description,
                        video_style=video_style,
                    )
                except Exception as exc:
                    self.log.emit(f"LLM-озаглавливание не удалось ({exc}); эвристические заголовки")
            except Exception as exc:
                self.log.emit(f"Эмбеддинги недоступны ({exc}); группирую сцены через LLM...")
                try:
                    lm_client.load_model(base_url, model, context_length)
                    scenes = scene_tools.call_lm_studio_for_segments(
                        base_url, model, t_segments, ttl_seconds=ttl_seconds,
                        style_anchor=style_anchor,
                        character_description=character_description,
                        video_style=video_style,
                    )
                except Exception as exc2:
                    self.log.emit(f"LLM-группировка не удалась ({exc2}); режу по длительности")
                    scenes = scene_tools.build_scenes_from_segments(t_segments)

            core_scenes = [Scene(**s.__dict__) for s in scenes]
            scenes_data = [s.__dict__ for s in core_scenes]
            self.project.write_json("scenes", scenes_data)
            report = write_validation_report(self.project)
            self.log.emit(validation_summary_message(report))
            self.log.emit(f"Готово: {len(core_scenes)} сцен")
            self.done.emit(core_scenes)
        except Exception as exc:
            self.failed.emit(str(exc))


class _ShotListWorker(QThread):
    log = Signal(str)
    done = Signal(list, list)
    failed = Signal(str)

    def __init__(self, project: Project, scenes: list[Scene], settings: dict):
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
            pipeline_segments = [TranscriptSegment(**s) for s in seg_data]
            style_anchor = self.settings.get("style_anchor") or self.project.metadata.get("style_anchor", "")
            character_description = self.settings.get("character_description") or self.project.metadata.get("character_description", "")
            video_style = self.settings.get("video_style") or self.project.metadata.get("video_style", "knitted")
            self.log.emit(f"Генерирую шот-лист через LM Studio ({len(pipeline_segments)} сегментов)...")
            ttl_seconds = lm_ttl_seconds(self.settings)
            reference_density = (
                str(self.settings.get("reference_density", "normal") or "normal")
                if self.settings.get("reference_enable", True)
                else "off"
            )
            shots = shot_planner.call_lm_studio_for_shots(
                base_url, model, pipeline_scenes, image_pref,
                segments=pipeline_segments, ttl_seconds=ttl_seconds,
                style_anchor=style_anchor,
                character_description=character_description,
                video_style=video_style,
                reference_density=reference_density,
            )

            core_shots = [Shot(**s.__dict__) for s in shots]
            shots_data = [s.__dict__ for s in core_shots]
            self.project.write_json("shot_list", shots_data)

            self.log.emit("Собираю доску ассетов...")
            cards = board_store.build_asset_board(self.project.dir)
            board_store.write_asset_board(self.project.dir, cards)
            report = write_validation_report(self.project)
            self.log.emit(validation_summary_message(report))
            core_cards = [AssetCard(**c.__dict__) for c in cards]
            self.log.emit(f"Готово: {len(core_shots)} кадров, {len(core_cards)} карточек")
            self.done.emit(core_shots, core_cards)
        except Exception as exc:
            self.failed.emit(str(exc))


class AutoPipeline(QObject):
    progress = Signal(str, str)
    step_done = Signal(str, object)
    all_done = Signal(list)
    failed = Signal(str)

    def __init__(self, project: Project, settings: dict, comfy_url: str, parent=None):
        super().__init__(parent)
        self.project = project
        self.settings = settings
        self.comfy_url = comfy_url
        self._scene_worker: _SceneAnalysisWorker | None = None
        self._shot_worker: _ShotListWorker | None = None
        self._unload_timer: QTimer | None = None

    def start(self):
        segments_data = self.project.read_json("transcript_segments")
        if not segments_data:
            self.failed.emit("Нет транскрипции. Сначала выполните транскрипцию.")
            return

        segments = [TranscriptSegment(**s) for s in segments_data]
        self.progress.emit("scenes", "Анализ сцен...")
        self._scene_worker = _SceneAnalysisWorker(self.project, segments, self.settings)
        self._scene_worker.log.connect(lambda msg: self.progress.emit("scenes", msg))
        self._scene_worker.done.connect(self._on_scenes_done)
        self._scene_worker.failed.connect(lambda err: self.failed.emit(f"Ошибка анализа сцен: {err}"))
        self._scene_worker.start()

    def _on_scenes_done(self, scenes: list[Scene]):
        self.step_done.emit("scenes", scenes)
        self.progress.emit("shots", "Генерация шот-листа...")
        self._shot_worker = _ShotListWorker(self.project, scenes, self.settings)
        self._shot_worker.log.connect(lambda msg: self.progress.emit("shots", msg))
        self._shot_worker.done.connect(self._on_shots_done)
        self._shot_worker.failed.connect(lambda err: self.failed.emit(f"Ошибка генерации шот-листа: {err}"))
        self._shot_worker.start()

    def _on_shots_done(self, shots: list[Shot], cards: list[AssetCard]):
        self.step_done.emit("shots", cards)
        self._schedule_lm_unload("shot list and asset board finished")
        self.all_done.emit(cards)

    def _schedule_lm_unload(self, reason: str):
        delay = lm_unload_delay_seconds(self.settings)
        if delay is None:
            self.progress.emit("shots", "Авто-выгрузка LM Studio выключена")
            return
        if self._unload_timer and self._unload_timer.isActive():
            self._unload_timer.stop()
        if delay <= 0:
            self._unload_lm_model(reason)
            return
        self.progress.emit("shots", f"LM Studio будет выгружена через {delay} сек.")
        self._unload_timer = QTimer(self)
        self._unload_timer.setSingleShot(True)
        self._unload_timer.timeout.connect(lambda: self._unload_lm_model(reason))
        self._unload_timer.start(delay * 1000)

    def _unload_lm_model(self, reason: str):
        base_url = self.settings.get("lm_url", "http://127.0.0.1:1234/v1")
        model = str(self.settings.get("lm_model", "local-model")).strip()
        if not model:
            self.progress.emit("shots", "LM Studio unload skipped: model id пустой")
            return
        try:
            result = lm_client.unload_model(base_url, model)
            self.progress.emit("shots", f"LM Studio выгружена: {result}")
        except Exception as exc:
            self.progress.emit("shots", f"LM Studio unload error: {lm_client.explain_error(exc)}")

    def cancel(self):
        if self._scene_worker and self._scene_worker.isRunning():
            self._scene_worker.terminate()
        if self._shot_worker and self._shot_worker.isRunning():
            self._shot_worker.terminate()
        if self._unload_timer and self._unload_timer.isActive():
            self._unload_timer.stop()
