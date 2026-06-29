from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from core.config import load_settings
from core.project import Project, list_projects, open_project


class StartScreen(QWidget):
    open_project_requested = Signal(str)
    new_project_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._projects: list[Project] = []
        self.build_ui()

    def build_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(24)

        # Logo / Title
        title = QLabel("🎬 Pipeline Studio")
        title.setStyleSheet("font-size: 42px; font-weight: 800; color: #f8fafc;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("Universal content production pipeline")
        subtitle.setStyleSheet("font-size: 16px; color: #94a3b8;")
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignCenter)
        btn_row.setSpacing(16)

        new_btn = QPushButton("➕ Новый проект")
        new_btn.setStyleSheet(
            "background: #22c55e; color: #000000; font-weight: 700; "
            "font-size: 16px; padding: 14px 40px; border-radius: 10px;"
        )
        new_btn.clicked.connect(self.new_project_requested.emit)
        btn_row.addWidget(new_btn)

        open_btn = QPushButton("📂 Открыть проект")
        open_btn.setStyleSheet(
            "background: #38bdf8; color: #000000; font-weight: 700; "
            "font-size: 16px; padding: 14px 40px; border-radius: 10px;"
        )
        open_btn.clicked.connect(self._open_project_dialog)
        btn_row.addWidget(open_btn)

        layout.addLayout(btn_row)

        # Recent projects
        recent_label = QLabel("Недавние проекты")
        recent_label.setStyleSheet("font-size: 18px; font-weight: 700; color: #94a3b8;")
        recent_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(recent_label)

        self.project_list = QListWidget()
        self.project_list.setMaximumWidth(600)
        self.project_list.setMinimumHeight(200)
        self.project_list.setStyleSheet(
            "QListWidget { background: #111827; border: 1px solid #1e293b; "
            "border-radius: 10px; padding: 8px; }"
            "QListWidget::item { padding: 12px 16px; border-radius: 6px; }"
            "QListWidget::item:selected { background: #1e3a5f; color: #38bdf8; }"
            "QListWidget::item:hover { background: #1e293b; }"
        )
        self.project_list.itemDoubleClicked.connect(self._on_item_activated)
        layout.addWidget(self.project_list, 0, Qt.AlignCenter)

        self.refresh()

    def refresh(self):
        self.project_list.clear()
        settings = load_settings()
        output_base = Path(settings.output_base or "projects")
        self._projects = list_projects(output_base)

        for proj in reversed(self._projects[-12:]):
            meta = proj.dir / ".project.json"
            info = ""
            if meta.exists():
                try:
                    import json
                    m = json.loads(meta.read_text("utf-8"))
                    preset = m.get("preset", "?")
                    lang = m.get("language", "?")
                    info = f"  ·  {preset}  ·  {lang}"
                except Exception:
                    pass
            text = f"📁 {proj.name}{info}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, str(proj.dir))
            item.setToolTip(str(proj.dir))
            self.project_list.addItem(item)

        if not self._projects:
            empty = QListWidgetItem("Создайте первый проект")
            empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            empty.setForeground(Qt.GlobalColor.gray)
            self.project_list.addItem(empty)

    def _on_item_activated(self, item: QListWidgetItem):
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.open_project_requested.emit(path)

    def _open_project_dialog(self):
        from PySide6.QtWidgets import QFileDialog
        settings = load_settings()
        folder = QFileDialog.getExistingDirectory(
            self,
            "Открыть проект",
            str(settings.output_base or "projects"),
        )
        if folder:
            self.open_project_requested.emit(folder)
