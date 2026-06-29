from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget, QMessageBox,
)

from core.project import Project
from ui.workers import TranscriberWorker


class TranscriptionTab(QWidget):
    transcription_completed = Signal(list)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.project: Project | None = None
        self.settings_provider = None
        self.worker: TranscriberWorker | None = None
        self.build_ui()

    def build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QLabel("Транскрипция аудио")
        header.setObjectName("heading")
        layout.addWidget(header)

        controls = QFrame()
        controls.setObjectName("panel")
        clayout = QHBoxLayout(controls)
        clayout.setContentsMargins(16, 16, 16, 16)
        clayout.setSpacing(12)

        self.audio_path = QLineEdit()
        self.audio_path.setPlaceholderText("Выберите аудиофайл (wav, mp3, m4a, flac, ogg)...")
        self.audio_path.setReadOnly(True)
        browse_btn = QPushButton("Обзор")
        browse_btn.clicked.connect(self.browse_audio)

        self.lang_combo = QComboBox()
        self.lang_combo.addItems(["ru", "en", "tr", "kk", "de", "fr", "es"])

        self.transcribe_btn = QPushButton("▶ Транскрибировать")
        self.transcribe_btn.setStyleSheet(
            "background: #38bdf8; color: #000000; font-weight: 700; padding: 8px 24px;"
        )
        self.transcribe_btn.clicked.connect(self.start_transcription)

        clayout.addWidget(QLabel("Аудио:"))
        clayout.addWidget(self.audio_path, 1)
        clayout.addWidget(browse_btn)
        clayout.addWidget(QLabel("Язык:"))
        clayout.addWidget(self.lang_combo)
        clayout.addWidget(self.transcribe_btn)
        layout.addWidget(controls)

        split = QFrame()
        split.setObjectName("panel")
        slayout = QHBoxLayout(split)
        slayout.setContentsMargins(16, 16, 16, 16)

        left = QVBoxLayout()
        left.addWidget(QLabel("Превью транскрипта"))
        self.transcript_preview = QPlainTextEdit()
        self.transcript_preview.setReadOnly(True)
        self.transcript_preview.setPlaceholderText("Здесь появится распознанный текст...")
        left.addWidget(self.transcript_preview)

        right = QVBoxLayout()
        right.addWidget(QLabel("Лог"))
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText("Лог транскрипции...")
        right.addWidget(self.log_output)

        slayout.addLayout(left, 1)
        slayout.addLayout(right, 1)
        layout.addWidget(split, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        layout.addWidget(self.status_label)

    def set_settings_provider(self, provider):
        self.settings_provider = provider

    def load_project(self, project: Project):
        self.project = project
        # Load existing transcript if available
        transcript = project.file_path("transcript")
        if transcript and transcript.exists():
            self.transcript_preview.setPlainText(
                transcript.read_text("utf-8", errors="replace")
            )

    def browse_audio(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите аудиофайл",
            "",
            "Audio (*.wav *.mp3 *.m4a *.flac *.ogg *.aac *.opus);;All (*.*)",
        )
        if path:
            self.audio_path.setText(path)

    def start_transcription(self):
        if not self.project:
            QMessageBox.warning(self, "Ошибка", "Сначала откройте проект")
            return
        audio = Path(self.audio_path.text().strip())
        if not audio.exists():
            QMessageBox.warning(self, "Ошибка", "Аудиофайл не найден")
            return

        settings = self.settings_provider() if self.settings_provider else {}
        settings["whisper_language"] = self.lang_combo.currentText()

        self.worker = TranscriberWorker(self.project, audio, settings)
        self.worker.log.connect(self._on_log)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.transcribe_btn.setEnabled(False)
        self.transcribe_btn.setText("⏳ Транскрибируем...")
        self.status_label.setText("Транскрипция...")
        self.log_output.clear()
        self.worker.start()

    def _on_log(self, message: str):
        self.log_output.appendPlainText(message)

    def _on_done(self, result: dict):
        self.transcribe_btn.setEnabled(True)
        self.transcribe_btn.setText("▶ Транскрибировать")
        self.transcript_preview.setPlainText(result["text"])
        self.status_label.setText(f"✅ Готово: {len(result['segments'])} сегментов, "
                                   f"текст: {len(result['text'])} символов")
        self.transcription_completed.emit(result["segments"])

    def _on_failed(self, error: str):
        self.transcribe_btn.setEnabled(True)
        self.transcribe_btn.setText("▶ Транскрибировать")
        self.status_label.setText(f"❌ Ошибка: {error}")
        QMessageBox.critical(self, "Ошибка транскрипции", error)
