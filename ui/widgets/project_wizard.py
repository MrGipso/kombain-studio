from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTextEdit, QVBoxLayout,
)

from core.project import Project, create_project


class ProjectWizard(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Новый проект")
        self.setModal(True)
        self.setMinimumSize(560, 360)

        self.project: Project | None = None
        self.build_ui()
        self.update_languages()

    def build_ui(self):
        layout = QVBoxLayout(self)

        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Имя проекта:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("my_video_project")
        name_layout.addWidget(self.name_edit, 1)
        layout.addLayout(name_layout)

        layout.addWidget(QLabel("Тип проекта:"))
        self.info_text = QTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setMaximumHeight(90)
        self.info_text.setPlainText("Универсальный проект: аудиодорожка анализируется на сцены, затем для каждого кадра генерируется AI-видеоряд. Presets и Remotion не используются.")
        layout.addWidget(self.info_text)

        lang_layout = QHBoxLayout()
        lang_layout.addWidget(QLabel("Язык:"))
        self.lang_combo = QComboBox()
        lang_layout.addWidget(self.lang_combo, 1)
        layout.addLayout(lang_layout)

        out_layout = QHBoxLayout()
        out_layout.addWidget(QLabel("Папка проектов:"))
        self.out_path = QLineEdit()
        self.out_path.setPlaceholderText("projects")
        out_layout.addWidget(self.out_path, 1)
        browse_btn = QPushButton("Обзор")
        browse_btn.clicked.connect(self.browse_output)
        out_layout.addWidget(browse_btn)
        layout.addLayout(out_layout)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Создать")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        layout.addWidget(self.buttons)

    def update_languages(self):
        self.lang_combo.clear()
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

        lang = self.lang_combo.currentData() or "ru"
        output_base = Path(self.out_path.text().strip() or "projects")

        try:
            self.project = create_project(
                name=name,
                output_base=output_base,
                settings=None,
                preset="universal",
            )
            self.project.settings.whisper_language = lang
            self.project.save_meta()
            super().accept()
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"Не удалось создать проект:\n{exc}")
