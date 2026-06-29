from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from core.project import Project
from core.models import AssetCard
from core.pipeline import PipelineContext


class DashboardTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.project: Project | None = None
        self.pipeline: PipelineContext | None = None
        self.build_ui()

    def build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        header = QLabel("Обзор проекта")
        header.setObjectName("heading")
        layout.addWidget(header)

        self.project_label = QLabel("Проект не загружен")
        self.project_label.setObjectName("muted")
        layout.addWidget(self.project_label)

        cards = QHBoxLayout()
        cards.setSpacing(14)

        self.metric_all = self._metric_card("Всего кадров", "0")
        self.metric_done = self._metric_card("Готово", "0", accent="#22c55e")
        self.metric_progress = self._metric_card("В процессе", "0", accent="#38bdf8")
        self.metric_planned = self._metric_card("Запланировано", "0", accent="#94a3b8")
        self.metric_failed = self._metric_card("Ошибок", "0", accent="#ef4444")

        cards.addWidget(self.metric_all)
        cards.addWidget(self.metric_done)
        cards.addWidget(self.metric_progress)
        cards.addWidget(self.metric_planned)
        cards.addWidget(self.metric_failed)
        layout.addLayout(cards)

        log_header = QLabel("Последние действия")
        log_header.setObjectName("heading")
        layout.addWidget(log_header)

        self.log_list = QListWidget()
        self.log_list.setMaximumHeight(200)
        layout.addWidget(self.log_list, 1)

    def _metric_card(self, title: str, value: str, accent: str | None = None):
        card = QFrame()
        card.setObjectName("panel")
        card.setMinimumSize(180, 120)
        clr = accent or "#f8fafc"
        card.setStyleSheet(
            f"QFrame#panel {{ border-left: 4px solid {clr}; }}"
        )
        clayout = QVBoxLayout(card)
        clayout.setContentsMargins(16, 14, 16, 14)
        clayout.setAlignment(Qt.AlignCenter)
        val = QLabel(value)
        val.setStyleSheet(f"font-size: 36px; font-weight: 800; color: {clr};")
        val.setAlignment(Qt.AlignCenter)
        lbl = QLabel(title)
        lbl.setObjectName("muted")
        lbl.setAlignment(Qt.AlignCenter)
        clayout.addWidget(val)
        clayout.addWidget(lbl)
        return card

    def load_project(self, project: Project, cards: list[AssetCard] | None = None):
        self.project = project
        self.project_label.setText(f"📁 {project.name}")
        cards = cards or []
        total = len(cards)
        done = sum(1 for c in cards if c.status == "generated" or c.approved)
        progress = sum(1 for c in cards if c.status == "generating")
        planned = sum(1 for c in cards if c.status == "planned")
        failed = sum(1 for c in cards if c.status in ("needs_review", "rejected"))

        self.metric_all.findChildren(QLabel)[0].setText(str(total))
        self.metric_done.findChildren(QLabel)[0].setText(str(done))
        self.metric_progress.findChildren(QLabel)[0].setText(str(progress))
        self.metric_planned.findChildren(QLabel)[0].setText(str(planned))
        self.metric_failed.findChildren(QLabel)[0].setText(str(failed))

    def add_log(self, message: str):
        self.log_list.addItem(message)
        self.log_list.scrollToBottom()
