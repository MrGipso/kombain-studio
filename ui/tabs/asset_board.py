import json
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QMessageBox,
)

from core.generation_queue import GenerationQueue
from core.models import AssetCard
from core.project import Project
from ui.workers import AssetGeneratorWorker
from ui.widgets.gen_queue import GenerationQueueWidget
try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
except ImportError:  # Qt Multimedia can be absent in some PySide builds.
    QAudioOutput = None
    QMediaPlayer = None
    QVideoWidget = None

STATUS_COLORS = {
    "planned": "#94a3b8",
    "image_ready": "#a78bfa",
    "generating": "#38bdf8",
    "generated": "#22c55e",
    "needs_review": "#f97316",
    "approved": "#22c55e",
    "rejected": "#ef4444",
    "skipped": "#64748b",
}


class AssetBoardTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.project: Project | None = None
        self.settings_provider = None
        self.cards: list[AssetCard] = []
        self.current_card_index: int | None = None
        self.worker: AssetGeneratorWorker | None = None
        self.preview_path: Path | None = None
        self.preview_player = None
        self.preview_audio = None
        self.preview_video_widget = None
        self.gen_queue = GenerationQueue(self)
        self.build_ui()

    def build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Доска кадров")
        title.setObjectName("heading")

        self.project_label = QLabel("Проект не загружен")
        self.project_label.setObjectName("muted")

        title_box.addWidget(title)
        title_box.addWidget(self.project_label)
        header.addLayout(title_box, 1)

        self.metrics_label = QLabel("Всего: 0 | Готово: 0 | В процессе: 0 | Ошибок: 0")
        self.metrics_label.setObjectName("metric")
        header.addWidget(self.metrics_label)

        self.comfy_url_input = QLineEdit("http://127.0.0.1:8188")
        self.comfy_url_input.setFixedWidth(240)
        header.addWidget(QLabel("ComfyUI:"))
        header.addWidget(self.comfy_url_input)
        layout.addLayout(header)

        # Vertical layout with draggable queue panel
        from PySide6.QtWidgets import QSizePolicy as SP

        self._vert_splitter = QSplitter(Qt.Vertical)
        self._vert_splitter.setHandleWidth(6)
        self._vert_splitter.setChildrenCollapsible(False)
        layout.addWidget(self._vert_splitter, 1)

        top_widget = QWidget()
        top_widget.setSizePolicy(SP.Expanding, SP.Expanding)
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Horizontal)
        top_layout.addWidget(splitter, 1)

        left = QFrame()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 14, 14)

        filter_bar = QHBoxLayout()
        self.filter_status = QComboBox()
        self.filter_status.addItems(
            ["Все", "planned", "image_ready", "generating", "generated", "needs_review", "approved", "rejected"]
        )
        self.filter_status.currentTextChanged.connect(self.refresh_table)

        self.filter_route = QComboBox()
        self.filter_route.addItems(
            ["Все роуты", "image+video", "remotion", "graphic"]
        )
        self.filter_route.currentTextChanged.connect(self.refresh_table)

        filter_bar.addWidget(QLabel("Статус:"))
        filter_bar.addWidget(self.filter_status)
        filter_bar.addWidget(QLabel("Роут:"))
        filter_bar.addWidget(self.filter_route)
        filter_bar.addStretch(1)

        self.refresh_btn = QPushButton("🔄")
        self.refresh_btn.setFixedWidth(36)
        self.refresh_btn.clicked.connect(self.reload_cards)
        filter_bar.addWidget(self.refresh_btn)
        left_layout.addLayout(filter_bar)

        self.shot_table = QTableWidget(0, 8)
        self.shot_table.setHorizontalHeaderLabels(
            ["Кадр", "Сц", "Время", "Статус", "Маршрут", "Тип", "Модель", "Готов"]
        )
        self.shot_table.verticalHeader().setVisible(False)
        self.shot_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.shot_table.setSelectionMode(QTableWidget.SingleSelection)
        self.shot_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.shot_table.setColumnWidth(0, 50)
        self.shot_table.setColumnWidth(1, 40)
        self.shot_table.setColumnWidth(2, 130)
        self.shot_table.setColumnWidth(3, 90)
        self.shot_table.setColumnWidth(4, 95)
        self.shot_table.setColumnWidth(5, 130)
        self.shot_table.horizontalHeader().setStretchLastSection(True)
        self.shot_table.itemSelectionChanged.connect(self.on_table_select)
        left_layout.addWidget(self.shot_table)

        batch_bar = QHBoxLayout()
        self.batch_image_btn = QPushButton("🖼️ Сгенерировать ВСЕ картинки")
        self.batch_image_btn.setStyleSheet(
            "background: #a78bfa; color: #000000; font-weight: 700;"
        )
        self.batch_image_btn.clicked.connect(self.batch_generate_images)

        self.batch_video_btn = QPushButton("🎬 Сгенерировать ВСЕ видео")
        self.batch_video_btn.setStyleSheet(
            "background: #f97316; color: #000000; font-weight: 700;"
        )
        self.batch_video_btn.clicked.connect(self.batch_generate_videos)

        self.batch_approve_btn = QPushButton("✅ Утвердить всё готовое")
        self.batch_approve_btn.setStyleSheet(
            "background: #22c55e; color: #000000; font-weight: 700;"
        )
        self.batch_approve_btn.clicked.connect(self.batch_approve)

        batch_bar.addWidget(self.batch_image_btn)
        batch_bar.addWidget(self.batch_video_btn)
        batch_bar.addWidget(self.batch_approve_btn)
        batch_bar.addStretch(1)
        left_layout.addLayout(batch_bar)
        splitter.addWidget(left)

        right = QFrame()
        right.setObjectName("panel")
        self.right_layout = QVBoxLayout(right)
        self.right_layout.setContentsMargins(16, 16, 16, 16)
        self.right_layout.setSpacing(8)

        self.card_title = QLabel("Выберите кадр")
        self.card_title.setObjectName("cardTitle")
        self.right_layout.addWidget(self.card_title)

        form = QHBoxLayout()
        form.setSpacing(8)

        col1 = QVBoxLayout()
        row1 = QHBoxLayout()
        self.status_combo = QComboBox()
        self.status_combo.addItems(
            ["planned", "generating", "generated", "needs_review", "approved", "rejected", "skipped"]
        )
        row1.addWidget(QLabel("Status:"))
        row1.addWidget(self.status_combo)
        col1.addLayout(row1)

        row2 = QHBoxLayout()
        self.route_combo = QComboBox()
        self.route_combo.addItems(["image+video", "remotion", "graphic"])
        row2.addWidget(QLabel("Route:"))
        row2.addWidget(self.route_combo)
        col1.addLayout(row2)
        form.addLayout(col1)

        col2 = QVBoxLayout()
        row3 = QHBoxLayout()
        self.image_combo = QComboBox()
        self.image_combo.addItems(["", "krea2_turbo", "ideogram4"])
        row3.addWidget(QLabel("Image:"))
        row3.addWidget(self.image_combo)
        col2.addLayout(row3)

        row4 = QHBoxLayout()
        self.video_combo = QComboBox()
        self.video_combo.addItems(["", "ltx2.3_i2v"])
        row4.addWidget(QLabel("Video:"))
        row4.addWidget(self.video_combo)
        col2.addLayout(row4)
        form.addLayout(col2)

        col3 = QVBoxLayout()
        self.screen_text = QLineEdit()
        self.screen_text.setPlaceholderText("Screen text...")
        col3.addWidget(QLabel("Screen text:"))
        col3.addWidget(self.screen_text)
        form.addLayout(col3, 1)

        self.right_layout.addLayout(form)

        self.description = QPlainTextEdit()
        self.description.setPlaceholderText("Description")
        self.description.setMaximumHeight(70)
        self.image_prompt = QPlainTextEdit()
        self.image_prompt.setPlaceholderText("Image prompt")
        self.image_prompt.setMaximumHeight(70)
        self.video_prompt = QPlainTextEdit()
        self.video_prompt.setPlaceholderText("Video prompt")
        self.video_prompt.setMaximumHeight(70)
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("Notes")
        self.notes.setMaximumHeight(60)

        self.right_layout.addWidget(QLabel("Description"))
        self.right_layout.addWidget(self.description)
        self.right_layout.addWidget(QLabel("Image prompt"))
        self.right_layout.addWidget(self.image_prompt)
        self.right_layout.addWidget(QLabel("Video prompt"))
        self.right_layout.addWidget(self.video_prompt)
        self.right_layout.addWidget(QLabel("Notes"))
        self.right_layout.addWidget(self.notes)

        self.outputs = QLabel("Outputs: not generated yet")
        self.outputs.setWordWrap(True)
        self.outputs.setObjectName("outputs")
        self.right_layout.addWidget(self.outputs)

        self.preview_title = QLabel("Предпросмотр")
        self.preview_title.setObjectName("cardTitle")
        self.right_layout.addWidget(self.preview_title)

        self.preview_label = QLabel("Нет сгенерированного файла")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(190)
        self.preview_label.setStyleSheet("background: #0f172a; border: 1px solid #1e293b; color: #94a3b8;")
        self.right_layout.addWidget(self.preview_label)

        if QVideoWidget is not None:
            self.preview_video_widget = QVideoWidget()
            self.preview_video_widget.setMinimumHeight(190)
            self.preview_video_widget.hide()
            self.right_layout.addWidget(self.preview_video_widget)

        preview_actions = QHBoxLayout()
        self.preview_play_btn = QPushButton("▶ Предпросмотр")
        self.preview_play_btn.clicked.connect(self.toggle_preview_playback)
        self.preview_play_btn.setEnabled(False)
        self.open_output_btn = QPushButton("Открыть файл")
        self.open_output_btn.clicked.connect(self.open_preview_file)
        self.open_output_btn.setEnabled(False)
        preview_actions.addWidget(self.preview_play_btn)
        preview_actions.addWidget(self.open_output_btn)
        preview_actions.addStretch(1)
        self.right_layout.addLayout(preview_actions)

        actions = QHBoxLayout()
        actions.setSpacing(6)

        btn_cfg = [
            ("💾 Save", "#38bdf8", self.save_current_card),
            ("▶ Route", "#38bdf8", lambda: self.start_generation("route")),
            ("🖼️ Image", "#38bdf8", lambda: self.start_generation("image")),
            ("🎬 Video", "#38bdf8", lambda: self.start_generation("video")),
            ("🎨 Graphic", "#38bdf8", lambda: self.start_generation("graphic")),
            ("✅ Approve", "#22c55e", lambda: self.set_status("approved")),
            ("⚠️ Fix", "#f97316", lambda: self.set_status("needs_review")),
            ("⏭️ Skip", "#64748b", lambda: self.set_status("skipped")),
        ]
        for label, color, handler in btn_cfg:
            btn = QPushButton(label)
            btn.setStyleSheet(f"background: {color}; color: #000000; font-weight: 700; padding: 6px 14px;")
            btn.clicked.connect(handler)
            actions.addWidget(btn)

        self.right_layout.addLayout(actions)

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.right_layout.addWidget(self.status_label)
        splitter.addWidget(right)

        # Queue panel at bottom — draggable via splitter
        self.queue_widget = GenerationQueueWidget(self.gen_queue)
        self.queue_widget.setSizePolicy(SP.Expanding, SP.Expanding)
        self.gen_queue.item_status.connect(self._on_queue_item_status)
        self._vert_splitter.addWidget(top_widget)
        self._vert_splitter.addWidget(self.queue_widget)
        self._vert_splitter.setSizes([600, 250])

    def set_settings_provider(self, provider):
        self.settings_provider = provider

    def load_project(self, project: Project, cards: list[AssetCard] | None = None):
        self.project = project
        self.gen_queue.set_project(project)
        self.project_label.setText(f"📁 {project.name}")
        if cards:
            self.set_cards(cards)
        else:
            self.reload_cards()

    def set_cards(self, cards: list[AssetCard]):
        self.cards = cards
        self.project.write_json("asset_board", [c.__dict__ for c in cards])
        self.refresh_table()

    def reload_cards(self):
        if not self.project:
            return
        board_data = self.project.read_json("asset_board")
        if board_data is not None and len(board_data) > 0:
            try:
                self.cards = [AssetCard(**item) for item in board_data]
                self.refresh_table()
                if self.current_card_index is not None and self.current_card_index < len(self.cards):
                    self.update_preview(self.cards[self.current_card_index])
            except Exception as exc:
                self.status_label.setText(f"Error loading board: {exc}")
        elif board_data is not None:
            self.cards = []
            self.refresh_table()

    def refresh_table(self):
        self.shot_table.setRowCount(0)
        status_filter = self.filter_status.currentText()
        route_filter = self.filter_route.currentText()

        done = progress = errors = 0
        for card_index, card in enumerate(self.cards):
            if status_filter != "Все" and card.status != status_filter:
                continue
            if route_filter != "Все роуты" and card.route != route_filter:
                continue

            row = self.shot_table.rowCount()
            self.shot_table.insertRow(row)
            index_item = QTableWidgetItem(f"{card.shot_index:03d}")
            index_item.setData(Qt.UserRole, card_index)
            self.shot_table.setItem(row, 0, index_item)
            self.shot_table.setItem(row, 1, QTableWidgetItem(str(card.scene_index)))
            self.shot_table.setItem(row, 2, QTableWidgetItem(f"{card.start} - {card.end}"))

            status_item = QTableWidgetItem(card.status)
            color = STATUS_COLORS.get(card.status, "#94a3b8")
            status_item.setBackground(Qt.GlobalColor.transparent)
            status_item.setForeground(Qt.GlobalColor.white)
            self.shot_table.setItem(row, 3, status_item)

            self.shot_table.setItem(row, 4, QTableWidgetItem(card.route))
            self.shot_table.setItem(row, 5, QTableWidgetItem(card.visual_type))
            self.shot_table.setItem(row, 6, QTableWidgetItem(
                card.image_model or card.video_model or "-"
            ))
            self.shot_table.setItem(row, 7, QTableWidgetItem("✅" if card.approved else ""))

            if card.status == "generated" or card.approved:
                done += 1
            elif card.status == "generating":
                progress += 1
            elif card.status in ("needs_review", "rejected"):
                errors += 1

        self.metrics_label.setText(
            f"Всего: {len(self.cards)} | ✅ {done} | ⏳ {progress} | ❌ {errors}"
        )

    def on_table_select(self):
        rows = self.shot_table.selectedItems()
        if not rows:
            return
        row = rows[0].row()
        index_item = self.shot_table.item(row, 0)
        card_index = index_item.data(Qt.UserRole) if index_item is not None else row
        if card_index is not None and 0 <= card_index < len(self.cards):
            card = self.cards[card_index]
            self.current_card_index = card_index
            self.card_title.setText(
                f"Кадр {card.shot_index:03d} | Сцена {card.scene_index} | {card.scene_title}"
            )
            self.status_combo.setCurrentText(card.status)
            self.route_combo.setCurrentText(card.route)
            self.image_combo.setCurrentText(card.image_model)
            self.video_combo.setCurrentText(card.video_model)
            self.screen_text.setText(card.screen_text)
            self.description.setPlainText(card.description)
            self.image_prompt.setPlainText(card.image_prompt)
            self.video_prompt.setPlainText(card.video_prompt)
            self.notes.setPlainText(card.notes)

            outputs = []
            if card.image_output:
                outputs.append(f"🖼️ {card.image_output}")
            if card.video_output:
                outputs.append(f"🎬 {card.video_output}")
            if card.remotion_output:
                outputs.append(f"🎨 {card.remotion_output}")
            self.outputs.setText("\n".join(outputs) if outputs else "Outputs: not generated yet")
            self.update_preview(card)

    def _preview_candidate(self, card: AssetCard) -> tuple[Path | None, str]:
        for value, kind in (
            (card.video_output, "video"),
            (card.remotion_output, "video"),
            (card.image_output, "image"),
        ):
            if value:
                path = Path(value)
                if path.exists():
                    return path, kind
        return None, ""

    def update_preview(self, card: AssetCard):
        self.stop_preview()
        path, kind = self._preview_candidate(card)
        self.preview_path = path
        self.open_output_btn.setEnabled(path is not None)
        self.preview_play_btn.setEnabled(kind == "video" and QMediaPlayer is not None)

        if not path:
            if self.preview_video_widget:
                self.preview_video_widget.hide()
            self.preview_label.show()
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Нет сгенерированного файла")
            return

        if kind == "image":
            if self.preview_video_widget:
                self.preview_video_widget.hide()
            self.preview_label.show()
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                self.preview_label.setPixmap(QPixmap())
                self.preview_label.setText(f"Не удалось открыть картинку:\n{path}")
                return
            target_width = max(320, self.preview_label.width() or 420)
            scaled = pixmap.scaled(target_width, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.preview_label.setText("")
            self.preview_label.setPixmap(scaled)
            return

        if kind == "video" and QMediaPlayer is not None and self.preview_video_widget is not None:
            self.preview_label.hide()
            self.preview_video_widget.show()
            self.preview_player = QMediaPlayer(self)
            if QAudioOutput is not None:
                self.preview_audio = QAudioOutput(self)
                self.preview_audio.setVolume(0.0)
                self.preview_player.setAudioOutput(self.preview_audio)
            self.preview_player.setVideoOutput(self.preview_video_widget)
            self.preview_player.setSource(QUrl.fromLocalFile(str(path)))
            self.preview_play_btn.setText("▶ Предпросмотр")
            return

        if self.preview_video_widget:
            self.preview_video_widget.hide()
        self.preview_label.show()
        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText(f"Видео готово:\n{path.name}\n\nНажмите 'Открыть файл'.")

    def stop_preview(self):
        if self.preview_player is not None:
            self.preview_player.stop()
            self.preview_player.deleteLater()
            self.preview_player = None
        if self.preview_audio is not None:
            self.preview_audio.deleteLater()
            self.preview_audio = None
        if hasattr(self, "preview_play_btn"):
            self.preview_play_btn.setText("▶ Предпросмотр")

    def toggle_preview_playback(self):
        if self.preview_player is None:
            return
        if self.preview_player.playbackState() == QMediaPlayer.PlayingState:
            self.preview_player.pause()
            self.preview_play_btn.setText("▶ Предпросмотр")
        else:
            self.preview_player.play()
            self.preview_play_btn.setText("⏸ Пауза")

    def open_preview_file(self):
        if self.preview_path and self.preview_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.preview_path)))

    def save_current_card(self):
        if self.current_card_index is None or self.current_card_index >= len(self.cards):
            return
        card = self.cards[self.current_card_index]
        card.status = self.status_combo.currentText()
        card.route = self.route_combo.currentText()
        card.image_model = self.image_combo.currentText()
        card.video_model = self.video_combo.currentText()
        card.screen_text = self.screen_text.text().strip()
        card.description = self.description.toPlainText().strip()
        card.image_prompt = self.image_prompt.toPlainText().strip()
        card.video_prompt = self.video_prompt.toPlainText().strip()
        card.notes = self.notes.toPlainText().strip()
        self.project.write_json("asset_board", [c.__dict__ for c in self.cards])
        self.refresh_table()
        self.status_label.setText("✅ Карточка сохранена")

    def set_status(self, status: str):
        if self.current_card_index is None or self.current_card_index >= len(self.cards):
            return
        self.cards[self.current_card_index].status = status
        self.project.write_json("asset_board", [c.__dict__ for c in self.cards])
        self.refresh_table()
        self.status_combo.setCurrentText(status)
        self.status_label.setText(f"Статус: {status}")

    def start_generation(self, mode: str):
        if self.current_card_index is None or self.current_card_index >= len(self.cards):
            QMessageBox.warning(self, "Ошибка", "Выберите кадр из таблицы")
            return
        if not self.project:
            return

        comfy_url = self.comfy_url_input.text().strip() or "http://127.0.0.1:8188"

        self.worker = AssetGeneratorWorker(
            self.project, self.current_card_index, mode, comfy_url
        )
        self.worker.log.connect(self._on_log)
        self.worker.done.connect(self._on_generation_done)
        self.worker.failed.connect(self._on_generation_failed)
        self.status_label.setText(f"⏳ Генерация {mode}...")
        self.worker.start()

    def _on_log(self, message: str):
        self.status_label.setText(message[:80])

    def _on_generation_done(self, cards):
        self.cards = [AssetCard(**c.__dict__) for c in cards]
        self.project.write_json("asset_board", [c.__dict__ for c in self.cards])
        self.refresh_table()
        if self.current_card_index is not None and self.current_card_index < len(self.cards):
            self.update_preview(self.cards[self.current_card_index])
        self.status_label.setText("✅ Генерация завершена")

    def _on_generation_failed(self, error: str):
        self.reload_cards()
        self.status_label.setText(f"❌ {error[:60]}...")
        QMessageBox.critical(self, "Ошибка генерации", error)

    def batch_generate_images(self):
        if not self.project:
            return
        planned = [i for i, c in enumerate(self.cards) if c.status == "planned"]
        if not planned:
            QMessageBox.information(self, "Готово", "Нет запланированных кадров")
            return
        comfy_url = self.comfy_url_input.text().strip() or "http://127.0.0.1:8188"
        self.gen_queue.set_comfy_url(comfy_url)
        self.gen_queue.add(planned, mode="image")
        self.status_label.setText(f"📦 {len(planned)} картинок добавлено в очередь")

    def batch_generate_videos(self):
        if not self.project:
            return
        ready = [i for i, c in enumerate(self.cards) if c.status == "image_ready"]
        if not ready:
            alt = [i for i, c in enumerate(self.cards) if c.status == "planned"]
            if alt:
                QMessageBox.information(self, "Нужны картинки", "Сначала сгенерируйте картинки (кнопка слева).")
            else:
                QMessageBox.information(self, "Готово", "Нет кадров со статусом image_ready")
            return
        comfy_url = self.comfy_url_input.text().strip() or "http://127.0.0.1:8188"
        self.gen_queue.set_comfy_url(comfy_url)
        self.gen_queue.add(ready, mode="video")
        self.status_label.setText(f"📦 {len(ready)} видео добавлено в очередь")

    def _on_queue_item_status(self, card_index: int, status: str):
        if status in ("completed", "failed"):
            self.reload_cards()

    def batch_approve(self):
        if not self.project:
            return
        for card_index, card in enumerate(self.cards):
            if card.status == "generated":
                card.approved = True
                card.status = "approved"
        self.project.write_json("asset_board", [c.__dict__ for c in self.cards])
        self.refresh_table()
        self.status_label.setText("✅ Готовые кадры утверждены")
