#!/usr/bin/env python3
"""Комбайн Studio — десктопная оболочка вокруг локального веб-приложения.

Поднимает тот же FastAPI-сервер, что и `start_web_server.bat`, дожидается его
готовности и показывает интерфейс в нативном окне вместо вкладки браузера.
Окно и сервер живут одним процессом: закрытие окна останавливает сервер.

Ничего в самом приложении не меняется — это только способ запуска. Обычный
запуск через браузер (`start_fast.bat`) продолжает работать как раньше.

Запуск:
    uv run --extra desktop-app python launch.py

Полезные переменные окружения:
    PIPELINE_HOST / PIPELINE_PORT  — адрес и порт локального сервера
    PIPELINE_DATA_DIR              — каталог изменяемого состояния
    PIPELINE_SETTINGS_PATH         — файл настроек
    PIPELINE_JOBS_DB               — база истории задач
    PIPELINE_DESKTOP_DEVTOOLS=1    — открыть инструменты разработчика в окне
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Установленная программа (PyInstaller): ресурсы только для чтения, Python,
# uv и npm на машине не нужны.
FROZEN = bool(getattr(sys, "frozen", False))
SMOKE = "--smoke" in sys.argv[1:]

APP_TITLE = "Комбайн Studio"
# Ниже этого окно становится бесполезным: доска материалов — двухколоночная.
MIN_WINDOW_SIZE = (1100, 700)
# Запасной размер, если экран определить не удалось.
FALLBACK_WINDOW_SIZE = (1600, 1000)
# Доля экрана при первом запуске: окно заметно меньше рабочего стола, поэтому
# видно, что это окно, а не полноэкранный режим — и его легко развернуть.
SCREEN_FILL_RATIO = 0.88
# Сервер стартует быстро, но первый импорт тяжёлых модулей на холодном диске
# занимает секунды. Ждём щедро, зато не открываем окно на пустое место.
STARTUP_TIMEOUT_SECONDS = 90.0


def _default_data_dir() -> Path:
    """Тот же каталог состояния, что задают .bat-скрипты."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "PipelineStudio"


def prepare_environment() -> Path:
    """Заполнить переменные окружения ровно как start_web_server.bat.

    Значения, заданные снаружи, не трогаем: запуск из .bat или из CI должен
    сохранять свои пути.
    """
    data_dir = Path(os.environ.get("PIPELINE_DATA_DIR") or _default_data_dir())
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["PIPELINE_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("PIPELINE_SETTINGS_PATH", str(data_dir / "settings.json"))
    os.environ.setdefault("PIPELINE_JOBS_DB", str(data_dir / "jobs.sqlite3"))

    # Разовый перенос старого файла настроек из корня репозитория.
    settings_path = Path(os.environ["PIPELINE_SETTINGS_PATH"])
    legacy_settings = ROOT / "content_pipeline_settings.json"
    if not settings_path.exists() and legacy_settings.is_file():
        settings_path.write_bytes(legacy_settings.read_bytes())
    return data_dir


def _documents_dir() -> Path:
    """Папка «Документы» пользователя (на Windows — с учётом её переноса)."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            CSIDL_PERSONAL = 5
            buffer = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PERSONAL, None, 0, buffer) == 0 and buffer.value:
                return Path(buffer.value)
        except Exception:  # noqa: BLE001 - запасной путь ниже
            pass
    documents = Path.home() / "Documents"
    return documents if documents.is_dir() else Path.home()


def frozen_workspace() -> Path:
    """Рабочая папка установленной программы: здесь лежат проекты.

    Код по умолчанию хранит проекты в ``projects`` относительно текущей
    папки. В установленной программе текущей папкой иначе был бы каталог
    установки — его нельзя трогать: он стирается при обновлении и удалении.
    """
    configured = os.environ.get("PIPELINE_WORKSPACE_DIR", "").strip()
    workspace = Path(configured).expanduser() if configured else _documents_dir() / APP_TITLE
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def configure_frozen_process(data_dir: Path) -> Path:
    """Подготовить процесс программы без консоли. Возвращает путь к журналу.

    * stdout/stderr у оконной программы равны None — пишем их в журнал, иначе
      uvicorn и print падают;
    * дочерние процессы (ffmpeg, whisper) без этого открывали бы по окну
      консоли на каждый вызов, а унаследованные пустые дескрипторы приводят
      к WinError 6 — поэтому по умолчанию CREATE_NO_WINDOW и DEVNULL.
    """
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"
    if sys.stdout is None or sys.stderr is None:
        stream = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - живёт до выхода
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream

    if sys.platform == "win32":
        import subprocess

        original_init = subprocess.Popen.__init__
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        def quiet_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | create_no_window
            for name in ("stdin", "stdout", "stderr"):
                if kwargs.get(name) is None:
                    kwargs[name] = subprocess.DEVNULL
            original_init(self, *args, **kwargs)

        subprocess.Popen.__init__ = quiet_init  # type: ignore[method-assign]
    return log_path


def frontend_is_built() -> bool:
    return (ROOT / "backend" / "static" / "index.html").is_file()


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) != 0


class ServerThread:
    """Uvicorn в фоновом потоке с корректной остановкой.

    Сервер запускается в этом же процессе, поэтому окно и API имеют общий
    жизненный цикл: закрытие окна снимает межпроцессные локи через штатный
    lifespan-выход, а не убийством процесса.
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._server = None
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def start(self) -> None:
        import uvicorn

        # Объект, а не строка "backend.main:app": так PyInstaller видит пакет
        # backend и кладёт его в установленную программу.
        from backend.main import app

        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="info",
            access_log=False,
            reload=False,
        )
        self._server = uvicorn.Server(config)

        def run() -> None:
            try:
                self._server.run()
            except BaseException as exc:  # noqa: BLE001 - показываем причину пользователю
                self.error = exc

        self._thread = threading.Thread(target=run, name="pipeline-studio-server", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stop(self, timeout: float = 15.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)


def wait_until_healthy(base_url: str, server: ServerThread, timeout: float) -> bool:
    """Опрашивать /api/health, пока сервер не ответит или не упадёт."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.error is not None or not server.alive:
            return False
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.25)
    return False


def _screen_size(webview) -> tuple[int, int] | None:
    """Определить размер основного экрана.

    Сначала спрашиваем pywebview (учитывает мультимонитор и масштабирование),
    затем — Windows API как запасной путь. Оба варианта необязательные: без
    них просто берётся фиксированный размер окна.
    """
    try:
        screens = getattr(webview, "screens", None) or []
        if screens:
            primary = screens[0]
            width, height = int(primary.width), int(primary.height)
            if width > 0 and height > 0:
                return width, height
    except Exception:  # noqa: BLE001 - определение экрана не критично
        pass

    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            # Просим реальные пиксели, иначе при масштабировании 125-150%
            # Windows вернёт уменьшенные логические размеры.
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:  # noqa: BLE001 - на старых системах недоступно
                pass
            width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
            if width > 0 and height > 0:
                return int(width), int(height)
        except Exception:  # noqa: BLE001
            pass
    return None


def default_window_size(screen: tuple[int, int] | None) -> tuple[int, int]:
    """Стартовый размер окна: чуть меньше экрана."""
    if not screen:
        return FALLBACK_WINDOW_SIZE
    screen_width, screen_height = screen
    width = max(MIN_WINDOW_SIZE[0], int(screen_width * SCREEN_FILL_RATIO))
    height = max(MIN_WINDOW_SIZE[1], int(screen_height * SCREEN_FILL_RATIO))
    # Не выходим за пределы экрана даже на маленьких мониторах.
    return min(width, screen_width), min(height, screen_height)


def centered_position(screen: tuple[int, int] | None, width: int, height: int) -> dict:
    """Координаты, ставящие окно ровно по центру экрана.

    Считается для ЛЮБОГО размера, а не только для расчётного: раньше позиция
    задавалась лишь при первом запуске, а дальше применялся сохранённый размер
    без координат — и система выставляла окно со смещением, каскадом.
    """
    if not screen:
        return {}
    screen_width, screen_height = screen
    return {
        "x": max(0, (screen_width - width) // 2),
        "y": max(0, (screen_height - height) // 2),
    }


def _window_state_path(data_dir: Path) -> Path:
    return data_dir / "desktop-window.json"


def load_window_state(data_dir: Path) -> dict:
    """Прочитать сохранённый размер окна; повреждённый файл игнорируется."""
    try:
        raw = json.loads(_window_state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    state: dict = {}
    width, height = raw.get("width"), raw.get("height")
    if isinstance(width, int) and isinstance(height, int):
        state["width"] = max(MIN_WINDOW_SIZE[0], min(width, 10_000))
        state["height"] = max(MIN_WINDOW_SIZE[1], min(height, 10_000))
    if isinstance(raw.get("maximized"), bool):
        state["maximized"] = raw["maximized"]
    return state


def save_window_state(data_dir: Path, window) -> None:
    """Сохранить размер окна. Это удобство, поэтому любые сбои проглатываем."""
    try:
        payload = {"width": int(window.width), "height": int(window.height)}
        _window_state_path(data_dir).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - неудачное сохранение не должно мешать выходу
        pass


def focus_existing_window() -> bool:
    """Вывести на передний план уже открытое окно приложения (Windows).

    Так повторный запуск ведёт себя как у обычной программы: не ругается и не
    плодит второе окно, а показывает существующее. На других системах молча
    возвращает False — там останется текстовое сообщение.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        handle = user32.FindWindowW(None, APP_TITLE)
        if not handle:
            return False
        SW_RESTORE = 9
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, SW_RESTORE)
        user32.SetForegroundWindow(handle)
        return True
    except Exception:  # noqa: BLE001 - не смогли сфокусировать, покажем сообщение
        return False


def smoke_check(base_url: str, server: ServerThread) -> int:
    """Проверка сборки без окна: сервер поднялся и отдаёт интерфейс и API.

    Запускается в CI на собранной программе (``--smoke``), чтобы установщик не
    выходил с программой, которая не стартует.
    """
    import urllib.request

    try:
        for path in ("/api/build-info", "/api/readiness", "/"):
            with urllib.request.urlopen(f"{base_url}{path}", timeout=10) as response:
                body = response.read()
                print(f"smoke {path}: {response.status} {body[:200]!r}", flush=True)
    except Exception as exc:  # noqa: BLE001 - сообщаем причину и выходим с ошибкой
        print(f"smoke failed: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        server.stop()
    return 0


def fail(message: str) -> int:
    """Сообщить о невозможности запуска.

    В тихом режиме (запуск из .vbs, без консоли) печатать некуда, поэтому
    показываем системное окно — иначе приложение просто не открылось бы молча.
    """
    text = f"{APP_TITLE}: {message}"
    print(f"\n{text}\n", file=sys.stderr)
    silent = FROZEN or os.environ.get("PIPELINE_DESKTOP_SILENT") == "1"
    if silent and not SMOKE and sys.platform == "win32":
        try:
            import ctypes

            MB_ICONERROR = 0x10
            ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, MB_ICONERROR)
        except Exception:  # noqa: BLE001 - сообщение уже ушло в stderr
            pass
    return 1


def main() -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    data_dir = prepare_environment()
    if FROZEN:
        configure_frozen_process(data_dir)
        os.chdir(frozen_workspace())
    else:
        os.chdir(ROOT)

    try:
        import webview
    except ImportError:
        return fail(
            "не установлен pywebview — библиотека нативного окна.\n"
            "  Установите:  uv sync --extra desktop-app\n"
            "  Или запустите обычную веб-версию:  start_fast.bat"
        )

    # Пересобираем интерфейс, если его исходники изменились (git pull, новая
    # версия поверх старой) — иначе окно показало бы прежний интерфейс.
    # В установленной программе интерфейс уже собран и исходников нет.
    if not FROZEN:
        from tools.frontend_build import ensure_built

        ensure_built()
    if not frontend_is_built():
        return fail(
            "интерфейс не собран (нет backend/static/index.html).\n"
            "  Установите Node.js и соберите:  python tools/frontend_build.py"
        )

    host = os.environ.get("PIPELINE_HOST", "127.0.0.1")
    port = int(os.environ.get("PIPELINE_PORT", "8000"))
    base_url = f"http://{host}:{port}"

    if not port_is_free(host, port):
        # Приложение уже запущено: показываем его окно и выходим без ошибки —
        # ровно как ведёт себя обычная программа при повторном клике по ярлыку.
        if focus_existing_window():
            return 0
        return fail(
            f"порт {port} уже занят — вероятно, приложение уже запущено.\n"
            f"  Откройте существующее окно или закройте прошлый запуск.\n"
            f"  Другой порт:  set PIPELINE_PORT=8010"
        )

    server = ServerThread(host, port)
    server.start()

    if not wait_until_healthy(base_url, server, STARTUP_TIMEOUT_SECONDS):
        server.stop()
        if server.error is not None:
            # Самая частая причина — межпроцессный лок: второй экземпляр на тот
            # же каталог проектов намеренно падает, а не портит общее состояние.
            return fail(f"сервер не запустился: {server.error}")
        return fail("сервер не ответил вовремя. Подробности в выводе выше.")

    if SMOKE:
        return smoke_check(base_url, server)

    screen = _screen_size(webview)
    # Осознанно выбранный пользователем размер важнее расчётного: если окно уже
    # подгоняли под себя, при следующем запуске оно откроется таким же.
    state = load_window_state(data_dir)
    if "width" in state and "height" in state:
        width, height = state["width"], state["height"]
    else:
        width, height = default_window_size(screen)
    # Позиция НЕ запоминается намеренно: окно всегда открывается по центру.
    window = webview.create_window(
        APP_TITLE,
        base_url,
        width=width,
        height=height,
        min_size=MIN_WINDOW_SIZE,
        text_select=True,
        confirm_close=False,  # несохранённые правки уже стережёт сама страница
        **centered_position(screen, width, height),
    )

    def on_closing() -> None:
        save_window_state(data_dir, window)

    window.events.closing += on_closing

    try:
        webview.start(debug=os.environ.get("PIPELINE_DESKTOP_DEVTOOLS") == "1")
    finally:
        # Штатная остановка: lifespan освобождает локи и закрывает базу задач.
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
