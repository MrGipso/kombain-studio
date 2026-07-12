import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QUrl

from core import board as board_store
from core import comfy_generation
from core import lm as lm_client
from core.config import load_settings, save_settings
from core.models import AssetCard


ROOT = Path(__file__).resolve().parent
PROJECTS_DIR = ROOT / "projects"


STATUS_LABELS = {
    "planned": "Planned",
    "generating": "Generating",
    "generated": "Generated",
    "needs_review": "Needs review",
    "approved": "Approved",
    "rejected": "Rejected",
    "skipped": "Skipped",
}


class AssetWorker(QThread):
    log = Signal(str)
    done = Signal()
    failed = Signal(str)

    def __init__(self, project_dir: Path, card_index: int, mode: str, comfy_url: str):
        super().__init__()
        self.project_dir = project_dir
        self.card_index = card_index
        self.mode = mode
        self.comfy_url = comfy_url

    def run(self):
        try:
            cards = board_store.load_asset_board(self.project_dir)
            card = cards[self.card_index]
            card.status = "generating"
            board_store.write_asset_board(self.project_dir, cards)
            self.log.emit(f"Generating shot {card.shot_index:03d} via {self.mode}")
            settings = load_settings()

            if self.mode in {"route", "graphic"}:
                card.route = "image+video"
                card.visual_type = "ai_image_then_i2v"
                card.remotion_template = ""
                card.remotion_output = ""
                if not card.image_model:
                    card.image_model = board_store.preferred_realistic_image_model("Auto")
                if not card.video_model:
                    card.video_model = "ltx2.3_i2v"
                generated_any = False
                if card.image_model:
                    image_path = comfy_generation.generate_comfy_image(
                        self.comfy_url,
                        self.project_dir,
                        card,
                        settings=settings,
                    )
                    card.image_output = str(image_path)
                    generated_any = True
                if card.video_model:
                    video_path = comfy_generation.generate_comfy_ltx_video(
                        self.comfy_url,
                        self.project_dir,
                        card,
                        settings=settings,
                    )
                    card.video_output = str(video_path)
                    generated_any = True
                if not generated_any:
                    raise RuntimeError("AI video generation needs image and video models.")
            elif self.mode == "image":
                image_path = comfy_generation.generate_comfy_image(
                    self.comfy_url,
                    self.project_dir,
                    card,
                    settings=settings,
                )
                card.image_output = str(image_path)
            elif self.mode == "video":
                video_path = comfy_generation.generate_comfy_ltx_video(
                    self.comfy_url,
                    self.project_dir,
                    card,
                    settings=settings,
                )
                card.video_output = str(video_path)
            else:
                raise RuntimeError(f"Unknown mode: {self.mode}")

            card.status = "generated"
            card.approved = False
            board_store.write_asset_board(self.project_dir, cards)
            self.log.emit(f"Shot {card.shot_index:03d} generated")
            self.done.emit()
        except Exception as exc:
            try:
                cards = board_store.load_asset_board(self.project_dir)
                if 0 <= self.card_index < len(cards):
                    card = cards[self.card_index]
                    card.status = "needs_review"
                    card.notes = (card.notes + "\n" if card.notes else "") + f"Generation error: {exc}"
                    board_store.write_asset_board(self.project_dir, cards)
            except Exception:
                pass
            self.failed.emit(str(exc))

    def render_remotion(self, card: AssetCard, cards: list[AssetCard]):
        raise RuntimeError("Remotion rendering was removed; use AI video generation instead.")

    def render_motion_text(self, card: AssetCard, cards: list[AssetCard]):
        raise RuntimeError("Remotion motion-text rendering was removed; use AI video generation instead.")

    def render_remotion_job(self, card: AssetCard, cards: list[AssetCard], job: dict):
        raise RuntimeError("Remotion rendering was removed; use AI video generation instead.")


class LmUnloadWorker(QThread):
    log = Signal(str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, lm_url: str, lm_model: str, reason: str):
        super().__init__()
        self.lm_url = lm_url
        self.lm_model = lm_model
        self.reason = reason

    def run(self):
        try:
            self.log.emit(f"LM Studio unload: model={self.lm_model}, reason={self.reason}")
            result = lm_client.unload_model(self.lm_url, self.lm_model)
            self.done.emit(json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            self.failed.emit(lm_client.explain_error(exc))


class ContentPipelineQt(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Content Pipeline - Qt Production Studio")
        self.resize(1420, 900)
        self.project_dir: Path | None = None
        self.asset_cards: list[AssetCard] = []
        self.current_card_index: int | None = None
        self.worker: AssetWorker | None = None
        self.lm_unload_worker: LmUnloadWorker | None = None
        self.lm_unload_timer = QTimer(self)
        self.lm_unload_timer.setSingleShot(True)
        self.lm_unload_timer.timeout.connect(lambda: self.unload_lm_now("timer"))

        self.lm_url = QLineEdit("http://127.0.0.1:1234/v1")
        self.lm_model = QLineEdit("local-model")
        self.lm_context = QLineEdit("32768")
        self.lm_auto_unload = QCheckBox("Авто-выгрузка после LM-задачи")
        self.lm_auto_unload.setChecked(True)
        self.lm_unload_delay = QLineEdit("60")
        self.comfy_url = QLineEdit("http://127.0.0.1:8188")
        self.asset_comfy_url = QLineEdit("http://127.0.0.1:8188")
        self.load_settings()
        self.build_ui()
        self.apply_dark_theme()
        self.load_latest_project()

    def build_ui(self):
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(230)
        for item in [
            "Транскрибация",
            "Сцены / LM Studio",
            "Схемы",
            "Видеоряд",
            "Доска кадров",
            "Remotion",
            "Настройки",
            "Экспорт",
        ]:
            QListWidgetItem(item, self.sidebar)
        self.sidebar.setCurrentRow(4)
        self.sidebar.currentRowChanged.connect(self.switch_page)
        root_layout.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        root_layout.addWidget(self.stack, 1)
        self.stack.addWidget(self.placeholder_page("Транскрибация", "Этот раздел пока остается в классическом интерфейсе."))
        self.stack.addWidget(self.placeholder_page("Сцены / LM Studio", "Анализ сцен пока доступен в старом интерфейсе."))
        self.stack.addWidget(self.placeholder_page("Схемы", "Здесь позже появятся редактор схем и предпросмотр."))
        self.stack.addWidget(self.placeholder_page("Видеоряд", "Shot list читается из текущего проекта."))
        self.stack.addWidget(self.asset_board_page())
        self.stack.addWidget(self.placeholder_page("Remotion", "Рендер выбранных Remotion-карточек доступен на доске кадров."))
        self.stack.addWidget(self.settings_page())
        self.stack.addWidget(self.placeholder_page("Экспорт", "Финальная сборка будет следующим этапом."))
        self.stack.setCurrentIndex(4)

        self.setCentralWidget(root)

        toolbar = QToolBar("Project")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        open_action = QAction("Открыть проект", self)
        open_action.triggered.connect(self.choose_project)
        refresh_action = QAction("Обновить доску", self)
        refresh_action.triggered.connect(self.rebuild_asset_board)
        outputs_action = QAction("Открыть outputs", self)
        outputs_action.triggered.connect(self.open_outputs)
        toolbar.addAction(open_action)
        toolbar.addAction(refresh_action)
        toolbar.addAction(outputs_action)

    def placeholder_page(self, title: str, subtitle: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        heading = QLabel(title)
        heading.setObjectName("heading")
        text = QLabel(subtitle)
        text.setObjectName("muted")
        layout.addWidget(heading)
        layout.addWidget(text)
        layout.addStretch(1)
        return page

    def settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        heading = QLabel("Настройки")
        heading.setObjectName("heading")
        subtitle = QLabel("LM Studio, ComfyUI и управление видеопамятью.")
        subtitle.setObjectName("muted")
        layout.addWidget(heading)
        layout.addWidget(subtitle)

        lm_panel = QFrame()
        lm_panel.setObjectName("panel")
        lm_layout = QGridLayout(lm_panel)
        lm_layout.setContentsMargins(16, 16, 16, 16)
        lm_layout.setHorizontalSpacing(12)
        lm_layout.setVerticalSpacing(10)
        lm_title = QLabel("LM Studio")
        lm_title.setObjectName("cardTitle")
        lm_layout.addWidget(lm_title, 0, 0, 1, 4)
        lm_layout.addWidget(QLabel("URL"), 1, 0)
        lm_layout.addWidget(self.lm_url, 1, 1)
        lm_layout.addWidget(QLabel("Model"), 1, 2)
        lm_layout.addWidget(self.lm_model, 1, 3)
        lm_layout.addWidget(QLabel("Context"), 2, 0)
        lm_layout.addWidget(self.lm_context, 2, 1)
        lm_layout.addWidget(self.lm_auto_unload, 2, 2)
        lm_layout.addWidget(QLabel("Задержка выгрузки, сек"), 3, 0)
        lm_layout.addWidget(self.lm_unload_delay, 3, 1)
        save_button = QPushButton("Сохранить настройки")
        save_button.clicked.connect(self.save_settings)
        timer_button = QPushButton("Запустить таймер выгрузки")
        timer_button.clicked.connect(self.start_lm_unload_timer)
        unload_button = QPushButton("Выгрузить сейчас")
        unload_button.clicked.connect(lambda: self.unload_lm_now("manual"))
        lm_layout.addWidget(save_button, 4, 0)
        lm_layout.addWidget(timer_button, 4, 1)
        lm_layout.addWidget(unload_button, 4, 2)
        self.lm_unload_status = QLabel("Таймер выгрузки не активен")
        self.lm_unload_status.setObjectName("metric")
        lm_layout.addWidget(self.lm_unload_status, 5, 0, 1, 4)
        layout.addWidget(lm_panel)

        comfy_panel = QFrame()
        comfy_panel.setObjectName("panel")
        comfy_layout = QGridLayout(comfy_panel)
        comfy_layout.setContentsMargins(16, 16, 16, 16)
        comfy_title = QLabel("ComfyUI")
        comfy_title.setObjectName("cardTitle")
        comfy_layout.addWidget(comfy_title, 0, 0, 1, 2)
        comfy_layout.addWidget(QLabel("URL"), 1, 0)
        comfy_layout.addWidget(self.comfy_url, 1, 1)
        layout.addWidget(comfy_panel)
        layout.addStretch(1)
        return page

    def asset_board_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
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
        header.addWidget(QLabel("ComfyUI"))
        self.asset_comfy_url.setFixedWidth(260)
        header.addWidget(self.asset_comfy_url)
        layout.addLayout(header)

        self.metrics = QLabel("Всего: 0 | generated: 0 | approved: 0 | review: 0")
        self.metrics.setObjectName("metric")
        layout.addWidget(self.metrics)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, 1)

        left = QFrame()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(10)

        self.shot_table = QTableWidget(0, 7)
        self.shot_table.setHorizontalHeaderLabels(["Shot", "Scene", "Time", "Status", "Route", "Type", "Model"])
        self.shot_table.verticalHeader().setVisible(False)
        self.shot_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.shot_table.setSelectionMode(QTableWidget.SingleSelection)
        self.shot_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.shot_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.shot_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.shot_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.shot_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.shot_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.shot_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.shot_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.shot_table.itemSelectionChanged.connect(self.on_table_select)
        left_layout.addWidget(self.shot_table)
        splitter.addWidget(left)

        right = QFrame()
        right.setObjectName("panel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(16, 16, 16, 16)
        right_layout.setSpacing(10)

        self.card_title = QLabel("Выберите shot")
        self.card_title.setObjectName("cardTitle")
        right_layout.addWidget(self.card_title)

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.status_combo = QComboBox()
        self.status_combo.addItems(["planned", "generating", "generated", "needs_review", "approved", "rejected", "skipped"])
        self.route_combo = QComboBox()
        self.route_combo.addItems(["remotion", "image+video", "image", "broll_or_ai", "graphic", "manual"])
        self.image_combo = QComboBox()
        self.image_combo.addItems(["", "krea2_turbo", "ideogram4"])
        self.video_combo = QComboBox()
        self.video_combo.addItems(["", "ltx2.3_i2v"])
        self.screen_text = QLineEdit()
        form.addWidget(QLabel("Status"), 0, 0)
        form.addWidget(self.status_combo, 0, 1)
        form.addWidget(QLabel("Route"), 0, 2)
        form.addWidget(self.route_combo, 0, 3)
        form.addWidget(QLabel("Image"), 1, 0)
        form.addWidget(self.image_combo, 1, 1)
        form.addWidget(QLabel("Video"), 1, 2)
        form.addWidget(self.video_combo, 1, 3)
        form.addWidget(QLabel("Screen text"), 2, 0)
        form.addWidget(self.screen_text, 2, 1, 1, 3)
        right_layout.addLayout(form)

        self.description = self.text_editor(84)
        self.image_prompt = self.text_editor(116)
        self.video_prompt = self.text_editor(100)
        self.notes = self.text_editor(80)
        right_layout.addWidget(QLabel("Description"))
        right_layout.addWidget(self.description)
        right_layout.addWidget(QLabel("Image prompt"))
        right_layout.addWidget(self.image_prompt)
        right_layout.addWidget(QLabel("Video prompt"))
        right_layout.addWidget(self.video_prompt)
        right_layout.addWidget(QLabel("Notes"))
        right_layout.addWidget(self.notes)

        self.outputs = QLabel("Outputs: not generated yet")
        self.outputs.setWordWrap(True)
        self.outputs.setObjectName("outputs")
        right_layout.addWidget(self.outputs)

        actions = QHBoxLayout()
        for label, handler in [
            ("Save", self.save_current_card),
            ("Generate route", lambda: self.start_generation("route")),
            ("Generate graphic", lambda: self.start_generation("graphic")),
            ("Generate image", lambda: self.start_generation("image")),
            ("Generate LTX video", lambda: self.start_generation("video")),
            ("Approve", lambda: self.set_status("approved")),
            ("Needs fix", lambda: self.set_status("needs_review")),
            ("Open output", self.open_selected_output),
        ]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch(1)
        right_layout.addLayout(actions)
        splitter.addWidget(right)
        splitter.setSizes([760, 620])

        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        layout.addWidget(self.log)
        return page

    def text_editor(self, height: int) -> QPlainTextEdit:
        editor = QPlainTextEdit()
        editor.setFixedHeight(height)
        editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        return editor

    def switch_page(self, index: int):
        self.stack.setCurrentIndex(index)

    def load_settings(self):
        settings = load_settings()
        self.lm_url.setText(settings.lm_url or self.lm_url.text())
        self.lm_model.setText(settings.lm_model or self.lm_model.text())
        self.lm_context.setText(str(settings.lm_context or self.lm_context.text()))
        self.lm_auto_unload.setChecked(bool(settings.lm_auto_unload))
        self.lm_unload_delay.setText(str(settings.lm_unload_delay))
        self.comfy_url.setText(settings.comfy_url or self.comfy_url.text())
        self.asset_comfy_url.setText(settings.comfy_url or self.asset_comfy_url.text())

    def save_settings(self):
        settings = load_settings()
        settings.lm_url = self.lm_url.text().strip()
        settings.lm_model = self.lm_model.text().strip()
        settings.lm_context = self.lm_context.text().strip()
        settings.lm_auto_unload = self.lm_auto_unload.isChecked()
        settings.lm_unload_delay = self.lm_unload_delay.text().strip()
        settings.comfy_url = (
            self.comfy_url.text().strip() or self.asset_comfy_url.text().strip()
        )
        self.asset_comfy_url.setText(settings.comfy_url)
        save_settings(settings)
        self.append_log("Settings saved")

    def lm_unload_delay_seconds(self) -> int:
        try:
            return max(0, int(self.lm_unload_delay.text().strip()))
        except ValueError:
            return 60

    def start_lm_unload_timer(self):
        self.save_settings()
        delay = self.lm_unload_delay_seconds()
        if delay <= 0:
            self.unload_lm_now("timer immediate")
            return
        self.lm_unload_timer.start(delay * 1000)
        self.lm_unload_status.setText(f"LM Studio будет выгружена через {delay} сек.")
        self.append_log(f"LM Studio unload timer started: {delay}s")

    def unload_lm_now(self, reason: str):
        self.save_settings()
        if self.lm_unload_worker and self.lm_unload_worker.isRunning():
            QMessageBox.information(self, "Выгрузка уже идет", "Дождитесь завершения текущей выгрузки.")
            return
        model = self.lm_model.text().strip()
        if not model:
            QMessageBox.warning(self, "Нет model id", "Укажите LM Studio model id.")
            return
        self.lm_unload_worker = LmUnloadWorker(self.lm_url.text().strip(), model, reason)
        self.lm_unload_worker.log.connect(self.append_log)
        self.lm_unload_worker.done.connect(self.lm_unload_done)
        self.lm_unload_worker.failed.connect(self.lm_unload_failed)
        self.lm_unload_worker.start()
        self.lm_unload_status.setText("Выгружаю модель...")

    def lm_unload_done(self, result: str):
        self.append_log(f"LM Studio unload result: {result}")
        self.lm_unload_status.setText("Модель выгружена из памяти")

    def lm_unload_failed(self, message: str):
        self.append_log(f"LM Studio unload error: {message}")
        self.lm_unload_status.setText("Ошибка выгрузки LM Studio")
        QMessageBox.critical(self, "Ошибка выгрузки", message)

    def load_latest_project(self):
        if not PROJECTS_DIR.exists():
            return
        candidates = [path for path in PROJECTS_DIR.iterdir() if path.is_dir() and (path / "01_transcript_segments.json").exists()]
        if not candidates:
            return
        latest = max(candidates, key=lambda path: (path / "01_transcript_segments.json").stat().st_mtime)
        self.load_project(latest)

    def choose_project(self):
        folder = QFileDialog.getExistingDirectory(self, "Открыть проект", str(PROJECTS_DIR))
        if folder:
            self.load_project(Path(folder))

    def load_project(self, project_dir: Path):
        self.project_dir = project_dir
        self.project_label.setText(str(project_dir))
        if not (project_dir / "06_asset_board.json").exists():
            self.rebuild_asset_board()
        else:
            self.asset_cards = board_store.load_asset_board(project_dir)
            self.populate_table()

    def rebuild_asset_board(self):
        if not self.project_dir:
            QMessageBox.warning(self, "Нет проекта", "Сначала откройте проект.")
            return
        try:
            self.asset_cards = board_store.build_asset_board(self.project_dir)
            board_store.write_asset_board(self.project_dir, self.asset_cards)
            self.populate_table()
            self.append_log(f"Asset board rebuilt: {len(self.asset_cards)} shots")
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка доски кадров", str(exc))

    def populate_table(self):
        self.shot_table.setRowCount(len(self.asset_cards))
        counts: dict[str, int] = {}
        for row, card in enumerate(self.asset_cards):
            counts[card.status] = counts.get(card.status, 0) + 1
            model = card.image_model or card.video_model or card.remotion_template or "-"
            values = [
                f"{card.shot_index:03d}",
                f"{card.scene_index:02d}",
                f"{card.start}-{card.end}",
                STATUS_LABELS.get(card.status, card.status),
                card.route,
                card.visual_type,
                model,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, row)
                self.shot_table.setItem(row, column, item)
        self.metrics.setText(
            " | ".join(
                [
                    f"Всего: {len(self.asset_cards)}",
                    f"generated: {counts.get('generated', 0)}",
                    f"approved: {counts.get('approved', 0)}",
                    f"review: {counts.get('needs_review', 0)}",
                    f"planned: {counts.get('planned', 0)}",
                ]
            )
        )
        if self.asset_cards and self.current_card_index is None:
            self.shot_table.selectRow(0)

    def on_table_select(self):
        rows = self.shot_table.selectionModel().selectedRows()
        if not rows:
            return
        index = rows[0].row()
        if 0 <= index < len(self.asset_cards):
            self.current_card_index = index
            self.show_card(self.asset_cards[index])

    def show_card(self, card: AssetCard):
        self.card_title.setText(
            f"Shot {card.shot_index:03d} | Scene {card.scene_index:02d} | {card.start}-{card.end} | {card.route}"
        )
        self.set_combo(self.status_combo, card.status)
        self.set_combo(self.route_combo, card.route)
        self.set_combo(self.image_combo, card.image_model)
        self.set_combo(self.video_combo, card.video_model)
        self.screen_text.setText(card.screen_text)
        self.description.setPlainText(card.description)
        self.image_prompt.setPlainText(card.image_prompt)
        self.video_prompt.setPlainText(card.video_prompt)
        self.notes.setPlainText(card.notes)
        outputs = []
        if card.image_output:
            outputs.append(f"Image: {card.image_output}")
        if card.video_output:
            outputs.append(f"Video: {card.video_output}")
        if card.remotion_output:
            outputs.append(f"Remotion: {card.remotion_output}")
        self.outputs.setText("\n".join(outputs) if outputs else "Outputs: not generated yet")

    def set_combo(self, combo: QComboBox, value: str):
        index = combo.findText(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def save_current_card(self):
        if self.current_card_index is None or not self.project_dir:
            return
        card = self.asset_cards[self.current_card_index]
        card.status = self.status_combo.currentText()
        card.route = self.route_combo.currentText()
        card.image_model = self.image_combo.currentText()
        card.video_model = self.video_combo.currentText()
        card.screen_text = self.screen_text.text().strip()
        card.description = self.description.toPlainText().strip()
        card.image_prompt = self.image_prompt.toPlainText().strip()
        card.video_prompt = self.video_prompt.toPlainText().strip()
        card.notes = self.notes.toPlainText().strip()
        card.approved = card.status == "approved"
        board_store.write_asset_board(self.project_dir, self.asset_cards)
        row = self.current_card_index
        self.populate_table()
        self.shot_table.selectRow(row)

    def set_status(self, status: str):
        self.set_combo(self.status_combo, status)
        self.save_current_card()

    def start_generation(self, mode: str):
        if not self.project_dir or self.current_card_index is None:
            return
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Задача идет", "Дождитесь завершения текущей генерации.")
            return
        self.comfy_url.setText(self.asset_comfy_url.text().strip())
        self.save_settings()
        self.save_current_card()
        self.worker = AssetWorker(self.project_dir, self.current_card_index, mode, self.asset_comfy_url.text().strip())
        self.worker.log.connect(self.append_log)
        self.worker.failed.connect(self.generation_failed)
        self.worker.done.connect(self.generation_done)
        self.worker.start()
        self.append_log(f"Started: {mode}")

    def generation_done(self):
        self.append_log("Done")
        self.reload_board_keep_selection()

    def generation_failed(self, message: str):
        self.append_log(f"ERROR: {message}")
        QMessageBox.critical(self, "Ошибка генерации", message)
        self.reload_board_keep_selection()

    def reload_board_keep_selection(self):
        if not self.project_dir:
            return
        row = self.current_card_index
        self.asset_cards = board_store.load_asset_board(self.project_dir)
        self.populate_table()
        if row is not None and row < self.shot_table.rowCount():
            self.shot_table.selectRow(row)

    def open_outputs(self):
        if not self.project_dir:
            return
        path = self.project_dir / "renders"
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def open_selected_output(self):
        if self.current_card_index is None:
            return
        card = self.asset_cards[self.current_card_index]
        for value in [card.video_output, card.remotion_output, card.image_output]:
            if value and Path(value).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(value))))
                return
        self.open_outputs()

    def append_log(self, text: str):
        self.log.appendPlainText(text)

    def apply_dark_theme(self):
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #0b1020;
                color: #dce7f7;
                font-family: "Segoe UI";
                font-size: 10pt;
            }
            QToolBar {
                background: #0f172a;
                border: 0;
                spacing: 8px;
                padding: 8px;
            }
            QToolButton, QPushButton {
                background: #1d2b45;
                color: #e8f1ff;
                border: 1px solid #2d405f;
                border-radius: 6px;
                padding: 7px 10px;
            }
            QToolButton:hover, QPushButton:hover {
                background: #274061;
                border-color: #3b82f6;
            }
            QPushButton:pressed {
                background: #1d4ed8;
            }
            QListWidget {
                background: #08111f;
                border: 0;
                padding: 12px 8px;
                outline: 0;
            }
            QListWidget::item {
                color: #91a4bf;
                padding: 12px 12px;
                border-radius: 7px;
                margin: 2px 0;
            }
            QListWidget::item:selected {
                background: #2563eb;
                color: white;
            }
            QListWidget::item:hover {
                background: #17223a;
                color: #dce7f7;
            }
            QFrame#panel {
                background: #111827;
                border: 1px solid #243149;
                border-radius: 8px;
            }
            QLabel#heading {
                font-size: 20pt;
                font-weight: 700;
                color: #ffffff;
            }
            QLabel#cardTitle {
                font-size: 14pt;
                font-weight: 700;
                color: #ffffff;
            }
            QLabel#muted {
                color: #8ea4c2;
            }
            QLabel#metric, QLabel#outputs {
                background: #111827;
                border: 1px solid #243149;
                border-radius: 6px;
                color: #b8c7dd;
                padding: 8px;
            }
            QTableWidget {
                background: #0f172a;
                alternate-background-color: #111c31;
                gridline-color: #243149;
                border: 0;
                selection-background-color: #1d4ed8;
                selection-color: #ffffff;
            }
            QHeaderView::section {
                background: #17233a;
                color: #9fb0c8;
                border: 0;
                border-right: 1px solid #243149;
                padding: 8px;
                font-weight: 600;
            }
            QLineEdit, QComboBox, QPlainTextEdit {
                background: #0b1220;
                color: #e5eefc;
                border: 1px solid #2d405f;
                border-radius: 6px;
                padding: 6px;
                selection-background-color: #2563eb;
            }
            QComboBox::drop-down {
                border: 0;
                width: 24px;
            }
            QPlainTextEdit#log {
                background: #050a14;
                color: #94a3b8;
                border: 1px solid #1f2a40;
            }
            QSplitter::handle {
                background: #0b1020;
                width: 10px;
            }
            QScrollBar:vertical {
                background: #0b1020;
                width: 12px;
            }
            QScrollBar::handle:vertical {
                background: #334155;
                border-radius: 6px;
            }
            """
        )


def main():
    os.chdir(ROOT)
    app = QApplication(sys.argv)
    window = ContentPipelineQt()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())



