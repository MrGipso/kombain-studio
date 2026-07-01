from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QMessageBox,
)

from core.models import Scene, Shot
from core.project import Project
from ui.workers import ShotListWorker


class ShotsTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.project: Project | None = None
        self.settings_provider = None
        self.scenes_provider = None
        self.shots: list[Shot] = []
        self.worker: ShotListWorker | None = None
        self.build_ui()

    def build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QLabel("Список кадров")
        header.setObjectName("heading")
        layout.addWidget(header)

        controls = QFrame()
        controls.setObjectName("panel")
        clayout = QHBoxLayout(controls)
        clayout.setContentsMargins(16, 16, 16, 16)

        gen_btn = QPushButton("Создать список кадров и доску")
        gen_btn.setStyleSheet(
            "background: #38bdf8; color: #000000; font-weight: 700; padding: 8px 24px;"
        )
        gen_btn.clicked.connect(self.start_generation)

        self.rebuild_btn = QPushButton("📋 Пересобрать доску")
        self.rebuild_btn.setStyleSheet(
            "background: #22c55e; color: #000000; font-weight: 700; padding: 8px 20px;"
        )
        self.rebuild_btn.clicked.connect(self.rebuild_asset_board)

        clayout.addWidget(gen_btn)
        clayout.addSpacing(20)
        clayout.addWidget(self.rebuild_btn)
        clayout.addStretch(1)
        layout.addWidget(controls)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, 1)

        left = QFrame()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 14, 14)

        left_header = QLabel("Кадры")
        left_header.setObjectName("cardTitle")
        left_layout.addWidget(left_header)

        self.shot_table = QTableWidget(0, 5)
        self.shot_table.setHorizontalHeaderLabels(["#", "Сцена", "Время", "Тип", "Модель"])
        self.shot_table.verticalHeader().setVisible(False)
        self.shot_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.shot_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.shot_table.setColumnWidth(0, 50)
        self.shot_table.setColumnWidth(1, 55)
        self.shot_table.setColumnWidth(2, 140)
        self.shot_table.setColumnWidth(3, 160)
        self.shot_table.horizontalHeader().setStretchLastSection(True)
        self.shot_table.itemSelectionChanged.connect(self.on_shot_select)
        left_layout.addWidget(self.shot_table)

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        left_layout.addWidget(self.status_label)
        splitter.addWidget(left)

        right = QFrame()
        right.setObjectName("panel")
        self.right_layout = QVBoxLayout(right)
        self.right_layout.setContentsMargins(16, 16, 16, 16)

        right_header = QLabel("Детали кадра")
        right_header.setObjectName("cardTitle")
        self.right_layout.addWidget(right_header)

        self.shot_detail = QPlainTextEdit()
        self.shot_detail.setReadOnly(True)
        self.shot_detail.setPlaceholderText("Выберите кадр из таблицы...")
        self.right_layout.addWidget(self.shot_detail)
        splitter.addWidget(right)

    def set_settings_provider(self, provider):
        self.settings_provider = provider

    def set_scenes_provider(self, provider):
        self.scenes_provider = provider

    def on_asset_board_built(self, callback):
        self._board_callback = callback

    def load_project(self, project: Project):
        self.project = project
        shots_data = project.read_json("shot_list")
        if shots_data:
            try:
                shots = [Shot(**item) for item in shots_data]
                self.set_shots(shots)
            except Exception:
                pass

    def set_shots(self, shots: list[Shot]):
        self.shots = shots
        self.shot_table.setRowCount(0)
        for shot in shots:
            row = self.shot_table.rowCount()
            self.shot_table.insertRow(row)
            self.shot_table.setItem(row, 0, QTableWidgetItem(f"{shot.index:03d}"))
            self.shot_table.setItem(row, 1, QTableWidgetItem(str(shot.scene_index)))
            self.shot_table.setItem(row, 2, QTableWidgetItem(f"{shot.start} - {shot.end}"))
            self.shot_table.setItem(row, 3, QTableWidgetItem(shot.visual_type))
            self.shot_table.setItem(row, 4, QTableWidgetItem(
                shot.image_model or shot.video_model or "-"
            ))
        self.status_label.setText(f"✅ {len(shots)} кадров")

    def on_shot_select(self):
        rows = self.shot_table.selectedItems()
        if not rows:
            return
        row = rows[0].row()
        if 0 <= row < len(self.shots):
            s = self.shots[row]
            detail = (
                f"Кадр {s.index:03d} | Сцена {s.scene_index}\n"
                f"Time: {s.start} - {s.end}\n"
                f"Type: {s.visual_type}\n"
                f"Image model: {s.image_model or '-'}\n"
                f"Video model: {s.video_model or '-'}\n"
                f"Motion: {s.motion}\n"
                f"Priority: {s.priority}\n"
                f"Fallback: {s.fallback}\n"
                f"Screen text: {s.screen_text}\n"
                f"\nDescription:\n{s.description}\n"
                f"\nImage prompt:\n{s.image_prompt}\n"
                f"\nVideo prompt:\n{s.video_prompt}"
            )
            self.shot_detail.setPlainText(detail)

    def start_generation(self):
        if not self.project:
            QMessageBox.warning(self, "Ошибка", "Сначала откройте проект")
            return

        scenes = self.scenes_provider() if self.scenes_provider else None
        if not scenes:
            scenes_data = self.project.read_json("scenes")
            if not scenes_data:
                QMessageBox.warning(self, "Ошибка", "Нет сцен. Сначала выполните анализ сцен.")
                return
            scenes = [Scene(**s) for s in scenes_data]

        settings = self.settings_provider() if self.settings_provider else {}

        self.worker = ShotListWorker(self.project, scenes, settings)
        self.worker.log.connect(self._on_log)
        self.worker.shot_progress.connect(self._on_shot_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.status_label.setText("⏳ Генерация кадров...")
        self.shot_table.setRowCount(0)
        self.shots = []
        self.worker.start()

    def _on_log(self, message: str):
        pass

    def _on_shot_progress(self, shots: list[Shot]):
        for s in shots:
            row = self.shot_table.rowCount()
            self.shot_table.insertRow(row)
            self.shot_table.setItem(row, 0, QTableWidgetItem(f"{s.index:03d}"))
            self.shot_table.setItem(row, 1, QTableWidgetItem(str(s.scene_index)))
            self.shot_table.setItem(row, 2, QTableWidgetItem(f"{s.start} - {s.end}"))
            self.shot_table.setItem(row, 3, QTableWidgetItem(s.visual_type))
            self.shot_table.setItem(row, 4, QTableWidgetItem(
                s.image_model or s.video_model or "-"
            ))
        self.status_label.setText(f"⏳ {self.shot_table.rowCount()} кадров...")

    def _on_done(self, result: dict):
        self.set_shots(result["shots"])
        if result["cards"] and hasattr(self, "_board_callback"):
            self._board_callback(result["cards"])
        QMessageBox.information(self, "Готово",
            f"Создано {len(result['shots'])} кадров\n"
            f"Доска кадров: {len(result['cards'])} карточек")

    def _on_failed(self, error: str):
        self.status_label.setText(f"❌ {error}")
        QMessageBox.critical(self, "Ошибка", error)

    def rebuild_asset_board(self):
        if not self.project:
            return
        import content_pipeline_gui as pipeline
        try:
            cards = pipeline.build_asset_board(self.project.dir)
        except FileNotFoundError as e:
            QMessageBox.warning(self, "Нет данных", str(e) + "\n\nСначала создайте shot list (Generate shot list → Via LM Studio).")
            return
        pipeline.write_asset_board(self.project.dir, cards)
        from core.models import AssetCard
        core_cards = [AssetCard(**c.__dict__) for c in cards]
        if hasattr(self, "_board_callback"):
            self._board_callback(core_cards)
        QMessageBox.information(self, "Готово", f"Доска кадров: {len(core_cards)} карточек")
