from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from core.config import Settings, list_presets, load_settings, save_settings


class SettingsTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings: Settings = load_settings()
        self.build_ui()
        self.load_from_settings()

    def build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QLabel("Настройки")
        header.setObjectName("heading")
        layout.addWidget(header)

        layout.addWidget(self._build_lm_panel())
        layout.addWidget(self._build_comfy_panel())
        layout.addWidget(self._build_whisper_panel())
        layout.addWidget(self._build_project_panel())

        save_btn = QPushButton("Сохранить настройки")
        save_btn.setStyleSheet("background: #22c55e; color: #000000; font-weight: 700; padding: 10px 24px;")
        save_btn.clicked.connect(self.save_to_settings)
        layout.addWidget(save_btn)
        layout.addStretch(1)

    def _make_panel(self, title: str) -> tuple[QFrame, QGridLayout]:
        panel = QFrame()
        panel.setObjectName("panel")
        gl = QGridLayout(panel)
        gl.setContentsMargins(16, 16, 16, 16)
        gl.setHorizontalSpacing(12)
        gl.setVerticalSpacing(10)
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        gl.addWidget(title_label, 0, 0, 1, 6)
        return panel, gl

    def _browse_file(self, target: QLineEdit, pattern: str = "All (*.*)"):
        path, _ = QFileDialog.getOpenFileName(self, "Выберите файл", "", pattern)
        if path:
            target.setText(path)

    def _browse_dir(self, target: QLineEdit):
        path = QFileDialog.getExistingDirectory(self, "Выберите папку", target.text() or "")
        if path:
            target.setText(path)

    def _build_lm_panel(self):
        panel, gl = self._make_panel("LM Studio")
        self.lm_url = QLineEdit()
        self.lm_model = QLineEdit()
        self.lm_context = QLineEdit()
        self.lm_auto_unload = QCheckBox("Авто-выгрузка после LM-задачи")
        self.lm_unload_delay = QLineEdit()
        self.lm_unload_delay.setFixedWidth(100)

        gl.addWidget(QLabel("URL"), 1, 0)
        gl.addWidget(self.lm_url, 1, 1, 1, 2)
        gl.addWidget(QLabel("Model"), 1, 3)
        gl.addWidget(self.lm_model, 1, 4, 1, 2)

        gl.addWidget(QLabel("Context"), 2, 0)
        gl.addWidget(self.lm_context, 2, 1)
        gl.addWidget(self.lm_auto_unload, 2, 2, 1, 2)
        gl.addWidget(QLabel("Задержка (сек)"), 2, 4)
        gl.addWidget(self.lm_unload_delay, 2, 5)
        return panel

    def _build_comfy_panel(self):
        panel, gl = self._make_panel("ComfyUI")
        self.comfy_url = QLineEdit()

        self.krea_workflow = QLineEdit()
        krea_btn = QPushButton("...")
        krea_btn.setFixedWidth(32)
        krea_btn.clicked.connect(lambda: self._browse_file(self.krea_workflow, "JSON (*.json)"))

        self.ideogram_workflow = QLineEdit()
        ideogram_btn = QPushButton("...")
        ideogram_btn.setFixedWidth(32)
        ideogram_btn.clicked.connect(lambda: self._browse_file(self.ideogram_workflow, "JSON (*.json)"))

        self.ltx_workflow = QLineEdit()
        ltx_btn = QPushButton("...")
        ltx_btn.setFixedWidth(32)
        ltx_btn.clicked.connect(lambda: self._browse_file(self.ltx_workflow, "JSON (*.json)"))

        gl.addWidget(QLabel("URL"), 1, 0)
        gl.addWidget(self.comfy_url, 1, 1, 1, 5)
        gl.addWidget(QLabel("Krea workflow"), 2, 0)
        gl.addWidget(self.krea_workflow, 2, 1, 1, 4)
        gl.addWidget(krea_btn, 2, 5)
        gl.addWidget(QLabel("Ideogram workflow"), 3, 0)
        gl.addWidget(self.ideogram_workflow, 3, 1, 1, 4)
        gl.addWidget(ideogram_btn, 3, 5)
        gl.addWidget(QLabel("LTX workflow"), 4, 0)
        gl.addWidget(self.ltx_workflow, 4, 1, 1, 4)
        gl.addWidget(ltx_btn, 4, 5)
        return panel

    def _build_whisper_panel(self):
        panel, gl = self._make_panel("Whisper & FFmpeg")
        self.whisper_cli = QLineEdit()
        wc_btn = QPushButton("...")
        wc_btn.setFixedWidth(32)
        wc_btn.clicked.connect(lambda: self._browse_file(self.whisper_cli, "whisper-cli.exe (whisper-cli.exe)"))

        self.whisper_model = QLineEdit()
        wm_btn = QPushButton("...")
        wm_btn.setFixedWidth(32)
        wm_btn.clicked.connect(lambda: self._browse_file(self.whisper_model, "Model (*.bin)"))

        self.ffmpeg_path = QLineEdit()
        ff_btn = QPushButton("...")
        ff_btn.setFixedWidth(32)
        ff_btn.clicked.connect(lambda: self._browse_file(self.ffmpeg_path, "ffmpeg.exe (ffmpeg.exe)"))

        self.whisper_language = QComboBox()
        self.whisper_language.addItems(["ru", "en", "tr", "kk", "de", "fr", "es"])

        gl.addWidget(QLabel("whisper-cli.exe"), 1, 0)
        gl.addWidget(self.whisper_cli, 1, 1, 1, 3)
        gl.addWidget(wc_btn, 1, 4)
        gl.addWidget(QLabel("Model .bin"), 1, 5)
        gl.addWidget(self.whisper_model, 1, 6, 1, 2)
        gl.addWidget(wm_btn, 1, 8)

        gl.addWidget(QLabel("FFmpeg"), 2, 0)
        gl.addWidget(self.ffmpeg_path, 2, 1, 1, 3)
        gl.addWidget(ff_btn, 2, 4)
        gl.addWidget(QLabel("Language"), 2, 5)
        gl.addWidget(self.whisper_language, 2, 6)
        return panel

    def _build_project_panel(self):
        panel, gl = self._make_panel("Проект по умолчанию")
        self.output_base = QLineEdit()
        out_btn = QPushButton("...")
        out_btn.setFixedWidth(32)
        out_btn.clicked.connect(lambda: self._browse_dir(self.output_base))

        self.image_pref = QComboBox()
        self.image_pref.addItems(["Auto", "Krea", "Ideogram"])

        gl.addWidget(QLabel("Папка проектов"), 1, 0)
        gl.addWidget(self.output_base, 1, 1, 1, 3)
        gl.addWidget(out_btn, 1, 4)
        gl.addWidget(QLabel("Image pref."), 1, 5)
        gl.addWidget(self.image_pref, 1, 6)
        return panel

    def load_from_settings(self):
        s = self.settings
        self.lm_url.setText(s.lm_url)
        self.lm_model.setText(s.lm_model)
        self.lm_context.setText(s.lm_context)
        self.comfy_url.setText(s.comfy_url)
        self.krea_workflow.setText(s.comfy_krea_workflow)
        self.ideogram_workflow.setText(s.comfy_ideogram_workflow)
        self.ltx_workflow.setText(s.comfy_ltx_workflow)
        self.whisper_cli.setText(s.whisper_cli)
        self.whisper_model.setText(s.whisper_model)
        self.ffmpeg_path.setText(s.ffmpeg)
        self.whisper_language.setCurrentText(s.whisper_language)
        self.output_base.setText(s.output_base)
        self.lm_auto_unload.setChecked(s.lm_auto_unload)
        self.lm_unload_delay.setText(s.lm_unload_delay)
        self.image_pref.setCurrentText(
            s.image_model_preference.capitalize() if s.image_model_preference else "Auto"
        )

    def save_to_settings(self):
        self.settings.lm_url = self.lm_url.text().strip()
        self.settings.lm_model = self.lm_model.text().strip()
        self.settings.lm_context = self.lm_context.text().strip()
        self.settings.comfy_url = self.comfy_url.text().strip()
        self.settings.comfy_krea_workflow = self.krea_workflow.text().strip()
        self.settings.comfy_ideogram_workflow = self.ideogram_workflow.text().strip()
        self.settings.comfy_ltx_workflow = self.ltx_workflow.text().strip()
        self.settings.whisper_cli = self.whisper_cli.text().strip()
        self.settings.whisper_model = self.whisper_model.text().strip()
        self.settings.ffmpeg = self.ffmpeg_path.text().strip()
        self.settings.whisper_language = self.whisper_language.currentText()
        self.settings.output_base = self.output_base.text().strip()
        self.settings.lm_auto_unload = self.lm_auto_unload.isChecked()
        self.settings.lm_unload_delay = self.lm_unload_delay.text().strip()
        self.settings.image_model_preference = self.image_pref.currentText().lower()
        save_settings(self.settings)

    def get_settings_dict(self) -> dict:
        self.save_to_settings()
        return {
            "lm_url": self.lm_url.text().strip(),
            "lm_model": self.lm_model.text().strip(),
            "lm_context": self.lm_context.text().strip(),
            "comfy_url": self.comfy_url.text().strip(),
            "comfy_krea_workflow": self.krea_workflow.text().strip(),
            "comfy_ideogram_workflow": self.ideogram_workflow.text().strip(),
            "comfy_ltx_workflow": self.ltx_workflow.text().strip(),
            "whisper_cli": self.whisper_cli.text().strip(),
            "whisper_model": self.whisper_model.text().strip(),
            "ffmpeg": self.ffmpeg_path.text().strip(),
            "whisper_language": self.whisper_language.currentText(),
            "output_base": self.output_base.text().strip(),
            "image_model_preference": self.image_pref.currentText().lower(),
            "lm_auto_unload": self.lm_auto_unload.isChecked(),
            "lm_unload_delay": self.lm_unload_delay.text().strip(),
        }
