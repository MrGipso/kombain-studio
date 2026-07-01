from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMainWindow, QProgressBar,
    QStackedWidget, QStatusBar, QToolBar, QVBoxLayout, QWidget, QMessageBox,
)

from core.auto_pipeline import AutoPipeline
from core.config import load_settings, save_settings
from core.locale import set_default_locale
from core.project import Project, create_project, open_project, list_projects
from core.pipeline import PipelineContext
from core.models import AssetCard
from core.validation import write_validation_report
from ui.theme import apply_dark_theme
from ui.tabs.dashboard import DashboardTab
from ui.tabs.transcription import TranscriptionTab
from ui.tabs.scenes import ScenesTab
from ui.tabs.shots import ShotsTab
from ui.tabs.asset_board import AssetBoardTab
from ui.tabs.preview import PreviewTab
from ui.tabs.settings import SettingsTab
from ui.widgets.project_wizard import ProjectWizard
from ui.widgets.start_screen import StartScreen


TAB_NAMES = [
    "Обзор",
    "Транскрипция",
    "Сцены",
    "Кадры",
    "Доска кадров",
    "Просмотр",
    "Настройки",
]

TAB_ICONS = ["📊", "🎤", "📝", "🎬", "🖼️", "👁️", "⚙️"]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pipeline Studio")
        self.resize(1500, 960)

        self.settings = load_settings()
        self.project: Project | None = None
        self.pipeline: PipelineContext | None = None
        self.asset_cards: list[AssetCard] = []
        self.auto_pipeline: AutoPipeline | None = None

        self.build_ui()
        self.connect_cross_tab_signals()
        self.connect_start_screen()
        self.connect_transcription_signal()
        self.try_restore_last_project()

    def build_ui(self):
        self.main_stack = QStackedWidget()
        self.setCentralWidget(self.main_stack)

        # Page 0: project workspace
        workspace = QWidget()
        root_layout = QHBoxLayout(workspace)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(200)
        for i, name in enumerate(TAB_NAMES):
            icon = TAB_ICONS[i] if i < len(TAB_ICONS) else "  "
            item = QListWidgetItem(f"  {icon}  {name}")
            self.sidebar.addItem(item)
        self.sidebar.setCurrentRow(0)
        self.sidebar.currentRowChanged.connect(self.switch_tab)
        root_layout.addWidget(self.sidebar)

        self.stack = QStackedWidget()

        self.tab_dashboard = DashboardTab()
        self.tab_transcription = TranscriptionTab()
        self.tab_scenes = ScenesTab()
        self.tab_shots = ShotsTab()
        self.tab_asset_board = AssetBoardTab()
        self.tab_preview = PreviewTab()
        self.tab_settings = SettingsTab()

        self.stack.addWidget(self.tab_dashboard)
        self.stack.addWidget(self.tab_transcription)
        self.stack.addWidget(self.tab_scenes)
        self.stack.addWidget(self.tab_shots)
        self.stack.addWidget(self.tab_asset_board)
        self.stack.addWidget(self.tab_preview)
        self.stack.addWidget(self.tab_settings)
        self.stack.setCurrentIndex(0)

        root_layout.addWidget(self.stack, 1)

        self.main_stack.addWidget(workspace)

        # Page 1: start screen
        self.start_screen = StartScreen()
        self.main_stack.addWidget(self.start_screen)

        self.toolbar = QToolBar("Project")
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)

        new_action = QAction("📁 Новый проект", self)
        open_action = QAction("📂 Открыть проект", self)
        refresh_action = QAction("🔄 Обновить", self)
        validate_action = QAction("🧪 Проверить", self)
        self.toolbar.addAction(new_action)
        self.toolbar.addAction(open_action)
        self.toolbar.addAction(refresh_action)
        self.toolbar.addAction(validate_action)

        new_action.triggered.connect(self.new_project)
        open_action.triggered.connect(self.open_project_dialog)
        refresh_action.triggered.connect(self.refresh_project)
        validate_action.triggered.connect(self.validate_project_now)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("⚪ Проект не загружен")
        self.status_bar.addWidget(self.status_label)

    def connect_start_screen(self):
        self.start_screen.new_project_requested.connect(self.new_project)
        self.start_screen.open_project_requested.connect(self._open_project_path)

    def connect_transcription_signal(self):
        self.tab_transcription.transcription_completed.connect(self._on_transcription_completed)

    def _on_transcription_completed(self, segments):
        if not self.project:
            return
        reply = QMessageBox.question(
            self,
            "Автоматическая раскадровка",
            "Транскрипция завершена.\n\n"
            "Запустить автоматическую раскадровку?\n"
            "• Анализ сцен (LM Studio)\n"
            "• Генерация шот-листа\n"
            "• Подготовка к AI-видео (Krea2 + LTX)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._start_auto_pipeline()

    def _start_auto_pipeline(self):
        if not self.project:
            return
        settings_dict = self.tab_settings.get_settings_dict()
        comfy_url = settings_dict.get("comfy_url", "http://127.0.0.1:8188")

        self.auto_pipeline = AutoPipeline(
            project=self.project,
            settings=settings_dict,
            comfy_url=comfy_url,
            parent=self,
        )
        self.auto_pipeline.progress.connect(self._on_auto_progress)
        self.auto_pipeline.step_done.connect(self._on_auto_step_done)
        self.auto_pipeline.all_done.connect(self._on_auto_all_done)
        self.auto_pipeline.failed.connect(self._on_auto_failed)

        self.tab_transcription.status_label.setText("⏳ Автоматическая раскадровка...")
        self.auto_pipeline.start()

    def _on_auto_progress(self, step: str, message: str):
        step_names = {"scenes": "Анализ сцен", "shots": "Шот-лист", "generate": "Генерация"}
        label = step_names.get(step, step)
        self.tab_transcription.status_label.setText(f"⏳ {label}: {message}")

    def _on_auto_step_done(self, step: str, data):
        if step == "scenes" and isinstance(data, list):
            from core.models import Scene
            scenes = [Scene(**s.__dict__) if hasattr(s, '__dict__') else Scene(**s) for s in data]
            self.tab_scenes.set_scenes(scenes)
            self.sidebar.setCurrentRow(2)

    def _on_auto_all_done(self, cards: list[AssetCard]):
        self.asset_cards = cards
        self.tab_asset_board.load_project(self.project, cards)
        self.tab_dashboard.load_project(self.project, cards)
        self.tab_transcription.status_label.setText(
            f"✅ Раскадровка готова: {len(cards)} карточек. Переходите на Доску кадров."
        )
        self.sidebar.setCurrentRow(4)

    def _on_auto_failed(self, error: str):
        self.tab_transcription.status_label.setText(f"❌ Ошибка: {error}")
        QMessageBox.critical(self, "Ошибка автоматической раскадровки", error)

    def _open_project_path(self, path: str):
        project = open_project(Path(path), self.settings)
        if project:
            locale = project.load_locale()
            set_default_locale(locale)
            self.load_project(project)
        else:
            QMessageBox.warning(self, "Ошибка", "Не удалось открыть проект")

    def connect_cross_tab_signals(self):
        def settings_provider():
            return self.tab_settings.get_settings_dict()

        def segments_provider():
            if not self.project:
                return None
            data = self.project.read_json("transcript_segments")
            if not data:
                return None
            from core.models import TranscriptSegment
            return [TranscriptSegment(**s) for s in data]

        def scenes_provider():
            if not self.project:
                return None
            data = self.project.read_json("scenes")
            if not data:
                return None
            from core.models import Scene
            return [Scene(**s) for s in data]

        for tab in [self.tab_transcription, self.tab_scenes, self.tab_shots, self.tab_asset_board]:
            if hasattr(tab, "set_settings_provider"):
                tab.set_settings_provider(settings_provider)

        self.tab_scenes.set_segments_provider(segments_provider)
        self.tab_shots.set_scenes_provider(scenes_provider)

        self.tab_shots.on_asset_board_built(self._on_asset_board_built)

    def _on_asset_board_built(self, cards: list[AssetCard]):
        self.asset_cards = cards
        self.tab_asset_board.load_project(self.project, cards)
        self.tab_dashboard.load_project(self.project, cards)

    def switch_tab(self, index: int):
        if 0 <= index < self.stack.count():
            self.stack.setCurrentIndex(index)

    def new_project(self):
        wizard = ProjectWizard(self)
        if wizard.exec() == ProjectWizard.DialogCode.Accepted and wizard.project:
            project = wizard.project
            project.settings = self.settings
            project.settings.whisper_language = wizard.lang_combo.currentData() or "ru"
            project.settings.output_base = str(project.dir.parent)
            project.save_meta()
            self.load_project(project)

    def open_project_dialog(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Открыть проект",
            str(self.settings.output_base or "projects"),
        )
        if not folder:
            return
        project = open_project(Path(folder), self.settings)
        if project:
            locale = project.load_locale()
            set_default_locale(locale)
            self.load_project(project)
        else:
            QMessageBox.warning(self, "Ошибка", "Не удалось открыть проект")

    def load_project(self, project: Project):
        self.project = project
        self.pipeline = PipelineContext(project=project)
        locale = project.load_locale()
        set_default_locale(locale)
        self.setWindowTitle(f"Pipeline Studio — {project.name}")
        self.status_label.setText(f"📁 {project.name}")

        # Switch to workspace
        self.main_stack.setCurrentIndex(0)
        self.sidebar.setVisible(True)

        self.tab_dashboard.load_project(project)
        self.tab_transcription.load_project(project)
        self.tab_scenes.load_project(project)
        self.tab_shots.load_project(project)
        self.tab_preview.load_project(project)

        board_data = project.read_json("asset_board")
        if board_data and len(board_data) > 0:
            try:
                self.asset_cards = [AssetCard(**item) for item in board_data]
                self.tab_dashboard.load_project(project, self.asset_cards)
                self.tab_asset_board.load_project(project, self.asset_cards)
            except Exception:
                self.tab_asset_board.load_project(project)
                pass
        else:
            self.tab_asset_board.load_project(project)

        scenes_data = project.read_json("scenes")
        if scenes_data:
            try:
                from core.models import Scene
                scenes = [Scene(**item) for item in scenes_data]
                self.tab_scenes.set_scenes(scenes)
            except Exception:
                pass

        shots_data = project.read_json("shot_list")
        if shots_data:
            try:
                from core.models import Shot
                shots = [Shot(**item) for item in shots_data]
                self.tab_shots.set_shots(shots)
            except Exception:
                pass

    def validate_project_now(self):
        if not self.project:
            QMessageBox.information(self, "Проверка проекта", "Сначала откройте проект.")
            return
        try:
            report = write_validation_report(self.project)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка проверки", str(exc))
            return
        summary = report.get("summary", {})
        counts = summary.get("counts", {})
        issues = report.get("issues", [])
        lines = [
            f"Статус: {summary.get('status', 'unknown')}",
            f"Ошибки: {counts.get('error', 0)}",
            f"Предупреждения: {counts.get('warning', 0)}",
            "",
            "Отчет записан: 07_validation_report.json",
        ]
        if issues:
            lines.append("")
            lines.append("Первые проблемы:")
            for issue in issues[:8]:
                item = f" [{issue.get('item')}]" if issue.get("item") else ""
                lines.append(f"- {issue.get('level')} / {issue.get('stage')}{item}: {issue.get('message')}")
        QMessageBox.information(self, "Проверка проекта", "\n".join(lines))
        self.status_label.setText(
            f"Проверка: {summary.get('status', 'unknown')} | "
            f"errors={counts.get('error', 0)}, warnings={counts.get('warning', 0)}"
        )

    def refresh_project(self):
        if self.project:
            self.load_project(self.project)

    def try_restore_last_project(self):
        output_base = Path(self.settings.output_base or "projects")
        if output_base.exists():
            projects = list_projects(output_base)
            if projects:
                self.load_project(projects[-1])
                return
        # Show start screen
        self.main_stack.setCurrentIndex(1)
        self.sidebar.setVisible(False)
        self.start_screen.refresh()
