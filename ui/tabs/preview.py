from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QSlider, QTextEdit, QVBoxLayout, QWidget,
    QMessageBox,
)

from core.export import export_video
from core.project import Project


class ExportWorker(QThread):
    log = Signal(str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, project_dir: Path, ffmpeg_path: str, output_name: str):
        super().__init__()
        self.project_dir = project_dir
        self.ffmpeg_path = ffmpeg_path
        self.output_name = output_name

    def run(self):
        try:
            export_video(
                self.project_dir,
                ffmpeg_path=self.ffmpeg_path,
                log_callback=self._log,
                output_name=self.output_name,
            )
            output = self.project_dir / "exports" / self.output_name
            if output.exists():
                self.done.emit(str(output))
            else:
                self.failed.emit("Export did not produce output file.")
        except Exception as exc:
            self.failed.emit(str(exc))

    def _log(self, message: str):
        self.log.emit(message)


class PreviewTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.project: Project | None = None
        self.worker: ExportWorker | None = None
        self.build_ui()

    def build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QLabel("Просмотр и экспорт")
        header.setObjectName("heading")
        layout.addWidget(header)

        subtitle = QLabel("Финальная сборка видеоряда из утверждённых кадров")
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)

        # Player placeholder
        player = QFrame()
        player.setObjectName("panel")
        player.setMinimumHeight(300)
        player_layout = QVBoxLayout(player)
        player_layout.setAlignment(Qt.AlignCenter)
        self.player_placeholder = QLabel("🎬\nПревью плеера\n(утверждённые кадры)")
        self.player_placeholder.setAlignment(Qt.AlignCenter)
        self.player_placeholder.setStyleSheet("font-size: 24px; color: #94a3b8;")
        player_layout.addWidget(self.player_placeholder)
        layout.addWidget(player, 1)

        # Controls
        controls = QHBoxLayout()
        controls.setSpacing(12)

        self.export_btn = QPushButton("📦 Экспорт видео (concat)")
        self.export_btn.setStyleSheet(
            "background: #22c55e; color: #000000; font-weight: 700; padding: 10px 24px;"
        )
        self.export_btn.clicked.connect(self.start_export)
        controls.addWidget(self.export_btn)

        self.play_btn = QPushButton("▶ Открыть в плеере")
        self.play_btn.setStyleSheet(
            "background: #38bdf8; color: #000000; font-weight: 700; padding: 10px 24px;"
        )
        self.play_btn.clicked.connect(self.open_in_player)
        self.play_btn.setEnabled(False)
        controls.addWidget(self.play_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        controls.addWidget(self.progress_bar, 1)

        layout.addLayout(controls)

        # Export log
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumHeight(180)
        self.log_output.setPlaceholderText("Лог экспорта...")
        layout.addWidget(self.log_output)

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        layout.addWidget(self.status_label)

    def load_project(self, project: Project):
        self.project = project
        self.log_output.clear()
        self.play_btn.setEnabled(False)
        self.status_label.setText(f"Проект: {project.name}")

    def start_export(self):
        if not self.project:
            QMessageBox.warning(self, "Ошибка", "Сначала откройте проект.")
            return

        output_name, ok = QFileDialog.getSaveFileName(
            self,
            "Сохранить видео",
            str(self.project.exports_dir / "final_cut.mp4"),
            "MP4 видео (*.mp4)",
        )
        if not ok or not output_name:
            return

        ffmpeg = self._find_ffmpeg()
        if not ffmpeg:
            QMessageBox.critical(self, "Ошибка", "FFmpeg не найден. Проверьте настройки.")
            return

        self.export_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.log_output.clear()
        self.status_label.setText("⏳ Экспорт...")

        self.worker = ExportWorker(
            self.project.dir, ffmpeg, Path(output_name).name
        )
        self.worker.log.connect(self._on_log)
        self.worker.done.connect(self._on_export_done)
        self.worker.failed.connect(self._on_export_failed)
        self.worker.start()

    def _on_log(self, message: str):
        self.log_output.append(message)

    def _on_export_done(self, output_path: str):
        self.progress_bar.setVisible(False)
        self.export_btn.setEnabled(True)
        self.play_btn.setEnabled(True)
        self._last_export = output_path
        self.status_label.setText(f"✅ Экспорт завершён: {output_path}")

    def _on_export_failed(self, error: str):
        self.progress_bar.setVisible(False)
        self.export_btn.setEnabled(True)
        self.status_label.setText(f"❌ {error}")
        self.log_output.append(f"ERROR: {error}")

    def open_in_player(self):
        if hasattr(self, "_last_export") and self._last_export:
            import subprocess
            subprocess.Popen(["start", self._last_export], shell=True)

    def _find_ffmpeg(self) -> str:
        import shutil
        ff = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
        if ff:
            return ff
        if self.project and self.project.settings:
            path = self.project.settings.ffmpeg
            if path and Path(path).exists():
                return path
        return ""
