from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from core.config import list_presets, PresetConfig
from core.project import Project, create_project


class ProjectWizard(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Новый проект")
        self.setModal(True)
        self.setMinimumSize(640, 480)

        self._presets: list[PresetConfig] = []
        self.project: Project | None = None

        self.build_ui()
        self.refresh_presets()
        self.update_languages()

    def build_ui(self):
        layout = QVBoxLayout(self)

        # Name
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Имя проекта:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("my_video_project")
        name_layout.addWidget(self.name_edit, 1)
        layout.addLayout(name_layout)

        # Preset selector
        layout.addWidget(QLabel("Тип контента:"))
        self.preset_list = QListWidget()
        self.preset_list.setIconSize(self.preset_list.iconSize())
        self.preset_list.currentRowChanged.connect(self.on_preset_changed)
        layout.addWidget(self.preset_list, 2)

        # Language
        lang_layout = QHBoxLayout()
        lang_layout.addWidget(QLabel("Язык:"))
        self.lang_combo = QComboBox()
        lang_layout.addWidget(self.lang_combo, 1)
        layout.addLayout(lang_layout)

        # Output base
        out_layout = QHBoxLayout()
        out_layout.addWidget(QLabel("Папка проектов:"))
        self.out_path = QLineEdit()
        self.out_path.setPlaceholderText("projects")
        out_layout.addWidget(self.out_path, 1)
        browse_btn = QPushButton("Обзор")
        browse_btn.clicked.connect(self.browse_output)
        out_layout.addWidget(browse_btn)
        layout.addLayout(out_layout)

        # Preset info
        layout.addWidget(QLabel("Описание:"))
        self.info_text = QTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setMaximumHeight(100)
        layout.addWidget(self.info_text)

        # Buttons
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Создать")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        layout.addWidget(self.buttons)

    def refresh_presets(self):
        self._presets = list_presets()
        self.preset_list.clear()
        for p in self._presets:
            item = QListWidgetItem(p.name)
            info = p.raw.get("info", {})
            desc = info.get("description", "")
            item.setToolTip(desc)
            item.setData(Qt.ItemDataRole.UserRole, p.path.name)
            self.preset_list.addItem(item)
        if self._presets:
            self.preset_list.setCurrentRow(0)

    def on_preset_changed(self, row: int):
        if 0 <= row < len(self._presets):
            info = self._presets[row].raw.get("info", {})
            desc = info.get("description", "")
            self.info_text.setPlainText(desc)
            self.update_languages()

    def update_languages(self):
        self.lang_combo.clear()
        row = self.preset_list.currentRow()
        if 0 <= row < len(self._presets):
            locale_dir = self._presets[row].path / "locale"
            if locale_dir.exists():
                for f in sorted(locale_dir.iterdir()):
                    if f.suffix == ".json":
                        lang = f.stem
                        label = {"ru": "Русский", "en": "English"}.get(lang, lang)
                        self.lang_combo.addItem(f"{label} ({lang})", lang)
        if self.lang_combo.count() == 0:
            self.lang_combo.addItem("Русский (ru)", "ru")
            self.lang_combo.addItem("English (en)", "en")

    def browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Папка проектов")
        if folder:
            self.out_path.setText(folder)

    def accept(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Ошибка", "Введите имя проекта.")
            return

        row = self.preset_list.currentRow()
        if row < 0:
            QMessageBox.warning(self, "Ошибка", "Выберите тип контента.")
            return

        preset_path_name = self._presets[row].path.name
        lang = self.lang_combo.currentData() or "ru"
        output_base = Path(self.out_path.text().strip() or "projects")

        try:
            self.project = create_project(
                name=name,
                output_base=output_base,
                settings=None,
                preset=preset_path_name,
            )
            self.project.settings.whisper_language = lang
            self.project.save_meta()
            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"Не удалось создать проект:\n{exc}")
