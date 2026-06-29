from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QHeaderView, QLabel, QProgressBar,
    QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core.generation_queue import GenerationQueue, QueueItem


STATUS_COLORS = {
    "queued": "#94a3b8",
    "generating": "#38bdf8",
    "completed": "#22c55e",
    "failed": "#ef4444",
    "cancelled": "#64748b",
}

STATUS_LABELS = {
    "queued": "⏳ В очереди",
    "generating": "🔄 Генерация",
    "completed": "✅ Готово",
    "failed": "❌ Ошибка",
    "cancelled": "⏹️ Отменён",
}


class GenerationQueueWidget(QFrame):
    def __init__(self, queue: GenerationQueue, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._queue = queue
        self.build_ui()
        self._queue.queue_changed.connect(self.refresh)
        self.refresh()

    def sizeHint(self):
        return QSize(400, 200)

    def build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("📋 Очередь генерации")
        title.setObjectName("cardTitle")
        header.addWidget(title, 1)

        self.metrics = QLabel("0 в очереди | 0 готово | 0 ошибок")
        self.metrics.setObjectName("metric")
        header.addWidget(self.metrics)

        retry_btn = QPushButton("🔄 Повторить ошибки")
        retry_btn.clicked.connect(self._queue.retry_all_failed)
        header.addWidget(retry_btn)

        clear_btn = QPushButton("🗑️ Очистить готовые")
        clear_btn.clicked.connect(self._queue.clear_completed)
        header.addWidget(clear_btn)

        cancel_btn = QPushButton("⏹ Отменить всё")
        cancel_btn.clicked.connect(self._queue.cancel_all)
        header.addWidget(cancel_btn)

        layout.addLayout(header)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Кадр", "Сцена", "Статус", "Прогресс", "Ошибка", "Действия"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(1, 55)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(5, 160)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.table, 1)

    def refresh(self):
        items = self._queue.items
        self.table.setRowCount(0)

        for item in items:
            row = self.table.rowCount()
            self.table.insertRow(row)

            self.table.setItem(row, 0, QTableWidgetItem(f"{item.card_index:03d}"))
            self.table.setItem(row, 1, QTableWidgetItem(
                str(item.card.scene_index) if item.card is not None and hasattr(item.card, "scene_index") else "-"
            ))

            status_item = QTableWidgetItem(STATUS_LABELS.get(item.status, item.status))
            color = STATUS_COLORS.get(item.status, "#94a3b8")
            status_item.setForeground(Qt.GlobalColor.white)
            self.table.setItem(row, 2, status_item)

            progress = QProgressBar()
            progress.setMinimum(0)
            progress.setMaximum(100)
            if item.status == "generating":
                progress.setValue(int(item.progress * 100))
                progress.setStyleSheet(
                    "QProgressBar { height: 6px; background: #1e293b; border: none; border-radius: 3px; }"
                    "QProgressBar::chunk { background: #38bdf8; border-radius: 3px; }"
                )
            elif item.status == "completed":
                progress.setValue(100)
                progress.setStyleSheet(
                    "QProgressBar { height: 6px; background: #1e293b; border: none; border-radius: 3px; }"
                    "QProgressBar::chunk { background: #22c55e; border-radius: 3px; }"
                )
            else:
                progress.setValue(0)
            self.table.setCellWidget(row, 3, progress)

            err_text = item.error[:50] + "..." if item.error and len(item.error) > 50 else (item.error or "")
            self.table.setItem(row, 4, QTableWidgetItem(err_text))

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(4, 2, 4, 2)
            actions_layout.setSpacing(4)

            if item.status == "failed":
                retry_btn = QPushButton("🔄")
                retry_btn.setFixedWidth(32)
                retry_btn.clicked.connect(lambda checked, idx=item.card_index: self._queue.retry(idx))
                actions_layout.addWidget(retry_btn)

            if item.status == "generating":
                cancel_btn = QPushButton("⏹")
                cancel_btn.setFixedWidth(32)
                cancel_btn.clicked.connect(lambda checked, idx=item.card_index: self._queue.cancel(idx))
                actions_layout.addWidget(cancel_btn)

            if item.status in ("completed", "cancelled"):
                remove_btn = QPushButton("🗑️")
                remove_btn.setFixedWidth(32)
                remove_btn.clicked.connect(lambda checked, idx=item.card_index: self._queue.remove(idx))
                actions_layout.addWidget(remove_btn)

            actions_layout.addStretch(1)
            self.table.setCellWidget(row, 5, actions)

        self.metrics.setText(
            f"{self._queue.queued_count} в очереди | {self._queue.completed_count} готово | {self._queue.failed_count} ошибок"
        )
