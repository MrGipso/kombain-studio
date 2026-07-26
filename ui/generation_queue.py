import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, QTimer

from core.models import AssetCard
from core.project import Project
from ui.workers import AssetGeneratorWorker


@dataclass
class QueueItem:
    card_index: int
    status: str = "queued"
    error: str | None = None
    card: AssetCard | None = None
    worker: AssetGeneratorWorker | None = None
    progress: float = 0.0
    cancellation_requested: bool = False
    mode: str = "route"


class GenerationQueue(QObject):
    queue_changed = Signal()
    item_status = Signal(int, str)
    item_progress = Signal(int, float)
    item_log = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._project: Project | None = None
        self._items: list[QueueItem] = []
        self._current_index: int | None = None
        self._comfy_url: str = "http://127.0.0.1:8188"

    def set_project(self, project: Project | None):
        self._project = project
        self._items.clear()
        self._current_index = None
        self.queue_changed.emit()

    def set_comfy_url(self, url: str):
        self._comfy_url = url.strip() or "http://127.0.0.1:8188"

    def add(self, card_indices: list[int], mode: str = "route"):
        for idx in card_indices:
            if not any(item.card_index == idx and item.status in ("queued", "generating") for item in self._items):
                self._items.append(QueueItem(card_index=idx, mode=mode))
        self.queue_changed.emit()
        self._try_process_next()

    def remove(self, card_index: int):
        self._items = [item for item in self._items if item.card_index != card_index]
        self.queue_changed.emit()

    def retry(self, card_index: int):
        for item in self._items:
            if item.card_index == card_index and item.status in ("failed", "completed"):
                item.status = "queued"
                item.error = None
                item.worker = None
                item.progress = 0.0
                self.item_status.emit(card_index, "queued")
                self.queue_changed.emit()
                self._try_process_next()
                return

    def retry_all_failed(self):
        for item in self._items:
            if item.status == "failed":
                item.status = "queued"
                item.error = None
                item.worker = None
                item.progress = 0.0
        self.queue_changed.emit()
        self._try_process_next()

    def cancel(self, card_index: int):
        for item in self._items:
            if item.card_index == card_index and item.status == "generating":
                item.cancellation_requested = True
                if item.worker and item.worker.isRunning():
                    item.worker.requestInterruption()
                item.status = "cancelled"
                self.item_status.emit(card_index, "cancelled")
                self.queue_changed.emit()
                return

    def cancel_all(self):
        for item in self._items:
            if item.status in ("queued", "generating"):
                item.cancellation_requested = True
                if item.worker and item.worker.isRunning():
                    item.worker.requestInterruption()
                item.status = "cancelled"
                self.item_status.emit(item.card_index, "cancelled")
        if self._current_index is None:
            self._try_process_next()
        self.queue_changed.emit()

    def clear_completed(self):
        self._items = [item for item in self._items if item.status in ("queued", "generating")]
        self.queue_changed.emit()

    @property
    def items(self) -> list[QueueItem]:
        return list(self._items)

    @property
    def is_running(self) -> bool:
        return self._current_index is not None

    @property
    def queued_count(self) -> int:
        return sum(1 for item in self._items if item.status == "queued")

    @property
    def completed_count(self) -> int:
        return sum(1 for item in self._items if item.status == "completed")

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self._items if item.status == "failed")

    def _try_process_next(self):
        if self._current_index is not None or self._project is None:
            return
        for item in self._items:
            if item.status == "queued":
                self._current_index = item.card_index
                item.status = "generating"
                self.item_status.emit(item.card_index, "generating")
                self.queue_changed.emit()
                self._start_worker(item)
                return

    def _start_worker(self, item: QueueItem):
        if not self._project:
            return
        worker = AssetGeneratorWorker(
            self._project, item.card_index, item.mode, self._comfy_url
        )
        worker.log.connect(lambda msg: self.item_log.emit(item.card_index, msg))
        worker.done.connect(lambda cards: self._on_item_done(item, cards))
        worker.failed.connect(lambda err: self._on_item_failed(item, err))
        item.worker = worker
        worker.start()

    def _on_item_done(self, item: QueueItem, cards):
        if item.cancellation_requested:
            item.status = "cancelled"
            self.item_status.emit(item.card_index, "cancelled")
        else:
            item.status = "completed"
            item.error = None
            if cards and 0 <= item.card_index < len(cards):
                card_raw = cards[item.card_index]
                if hasattr(card_raw, '__dict__'):
                    item.card = AssetCard(**card_raw.__dict__)
                else:
                    item.card = card_raw
            self.item_status.emit(item.card_index, "completed")
        self._current_index = None
        self.queue_changed.emit()
        self._try_process_next()

    def _on_item_failed(self, item: QueueItem, error: str):
        if item.cancellation_requested:
            item.status = "cancelled"
            self.item_status.emit(item.card_index, "cancelled")
        else:
            item.status = "failed"
            item.error = error
            self.item_status.emit(item.card_index, "failed")
        self._current_index = None
        self.queue_changed.emit()
        self._try_process_next()
