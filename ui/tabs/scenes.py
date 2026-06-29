from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton,
    QSplitter, QVBoxLayout, QWidget, QMessageBox,
)

from core.models import Scene
from core.project import Project
from ui.workers import SceneAnalyzerWorker


class ScenesTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.project: Project | None = None
        self.settings_provider = None
        self.segments_provider = None
        self.scenes: list[Scene] = []
        self.worker: SceneAnalyzerWorker | None = None
        self.build_ui()

    def build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QLabel("Анализ сцен")
        header.setObjectName("heading")
        layout.addWidget(header)

        controls = QFrame()
        controls.setObjectName("panel")
        clayout = QHBoxLayout(controls)
        clayout.setContentsMargins(16, 16, 16, 16)

        self.analyze_btn = QPushButton("Анализировать сцены")
        self.analyze_btn.setStyleSheet(
            "background: #38bdf8; color: #000000; font-weight: 700; padding: 8px 24px;"
        )
        self.analyze_btn.clicked.connect(self.start_analysis)

        self.export_btn = QPushButton("📦 Экспортировать сцены")
        self.export_btn.clicked.connect(self.export_scenes)

        clayout.addWidget(self.analyze_btn)
        clayout.addSpacing(20)
        clayout.addWidget(self.export_btn)
        clayout.addStretch(1)
        layout.addWidget(controls)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, 1)

        left = QFrame()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 14, 14)

        left_header = QLabel("Список сцен")
        left_header.setObjectName("cardTitle")
        left_layout.addWidget(left_header)

        self.scene_list = QListWidget()
        self.scene_list.currentRowChanged.connect(self.on_scene_select)
        left_layout.addWidget(self.scene_list)
        splitter.addWidget(left)

        right = QFrame()
        right.setObjectName("panel")
        self.right_layout = QVBoxLayout(right)
        self.right_layout.setContentsMargins(16, 16, 16, 16)

        right_header = QLabel("Детали сцены")
        right_header.setObjectName("cardTitle")
        self.right_layout.addWidget(right_header)

        self.scene_detail = QPlainTextEdit()
        self.scene_detail.setReadOnly(True)
        self.scene_detail.setPlaceholderText("Выберите сцену из списка...")
        self.right_layout.addWidget(self.scene_detail)

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.right_layout.addWidget(self.status_label)
        splitter.addWidget(right)

    def set_settings_provider(self, provider):
        self.settings_provider = provider

    def set_segments_provider(self, provider):
        self.segments_provider = provider

    def load_project(self, project: Project):
        self.project = project
        scenes_data = project.read_json("scenes")
        if scenes_data:
            try:
                scenes = [Scene(**item) for item in scenes_data]
                self.set_scenes(scenes)
            except Exception:
                pass

    def set_scenes(self, scenes: list[Scene]):
        self.scenes = scenes
        self.scene_list.clear()
        for s in scenes:
            item = QListWidgetItem(f"{s.index:02d}. {s.title}  ({s.start} - {s.end})")
            self.scene_list.addItem(item)
        self.status_label.setText(f"✅ {len(scenes)} сцен(ы)")

    def on_scene_select(self, row: int):
        if 0 <= row < len(self.scenes):
            s = self.scenes[row]
            detail = (
                f"Title: {s.title}\n"
                f"Time: {s.start} - {s.end}\n"
                f"Visual direction: {s.visual_direction}\n"
                f"Screen text: {s.screen_text}\n"
                f"Music: {s.music_note}\n"
                f"Retention: {s.retention_note}\n"
                f"\nVoice text:\n{s.voice_text}"
            )
            self.scene_detail.setPlainText(detail)

    def start_analysis(self):
        if not self.project:
            QMessageBox.warning(self, "Ошибка", "Сначала откройте проект")
            return

        segments = self.segments_provider() if self.segments_provider else None
        if not segments:
            seg_data = self.project.read_json("transcript_segments")
            if not seg_data:
                QMessageBox.warning(self, "Ошибка", "Нет транскрипции. Сначала выполните транскрипцию.")
                return
            from core.models import TranscriptSegment
            segments = [TranscriptSegment(**s) for s in seg_data]

        settings = self.settings_provider() if self.settings_provider else {}

        self.worker = SceneAnalyzerWorker(self.project, segments, settings)
        self.worker.log.connect(self._on_log)
        self.worker.scene_progress.connect(self._on_scene_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.analyze_btn.setEnabled(False)
        self.analyze_btn.setText("⏳ Анализ...")
        self.status_label.setText("Анализируем сцены...")
        self.scene_list.clear()
        self.scenes = []
        self.worker.start()

    def _on_log(self, message: str):
        pass

    def _on_scene_progress(self, scene: Scene):
        self.scenes.append(scene)
        self.scene_list.addItem(
            QListWidgetItem(f"{scene.index:02d}. {scene.title}  ({scene.start} - {scene.end})")
        )
        self.status_label.setText(f"⏳ {len(self.scenes)} сцен...")

    def _on_done(self, scenes: list[Scene]):
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("Анализировать сцены")
        self.set_scenes(scenes)

    def _on_failed(self, error: str):
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("Анализировать сцены")
        self.status_label.setText(f"❌ {error}")
        QMessageBox.critical(self, "Ошибка анализа", error)

    def export_scenes(self):
        if not self.scenes:
            QMessageBox.warning(self, "Ошибка", "Нет сцен для экспорта")
            return
        if not self.project:
            return

        from core import pipeline as pipeline_module
        import content_pipeline_gui as old_pipeline

        pipeline_scenes = [
            old_pipeline.Scene(**s.__dict__) for s in self.scenes
        ]

        # Create edit plan CSV
        import csv
        csv_path = self.project.file_path("edit_plan")
        if csv_path:
            with open(str(csv_path), "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Scene", "Start", "End", "Title", "Visual Direction", "Screen Text"])
                for s in self.scenes:
                    writer.writerow([s.index, s.start, s.end, s.title,
                                     s.visual_direction, s.screen_text])

        # Write visual plan markdown
        md_path = self.project.file_path("visual_plan")
        if md_path:
            lines = ["# Visual Plan\n"]
            for s in self.scenes:
                lines.append(f"## Scene {s.index}: {s.title} ({s.start} - {s.end})")
                lines.append(f"- **Visual:** {s.visual_direction}")
                lines.append(f"- **Screen:** {s.screen_text}")
                lines.append(f"- **Music:** {s.music_note}")
                lines.append(f"- **Retention:** {s.retention_note}")
                lines.append("")
            md_path.write_text("\n".join(lines), "utf-8")

        QMessageBox.information(self, "Готово",
            f"Экспортировано {len(self.scenes)} сцен\n"
            f"CSV: {csv_path}\n"
            f"MD: {md_path}")
