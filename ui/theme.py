from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton, QTableWidget

DARK_PALETTE = {
    "bg": "#0b1020",
    "panel": "#111827",
    "panel_alt": "#172033",
    "text": "#f8fafc",
    "muted": "#94a3b8",
    "accent": "#38bdf8",
    "accent2": "#22c55e",
    "warning": "#f97316",
    "danger": "#ef4444",
    "border": "#1e293b",
    "input_bg": "#1e293b",
    "hover": "#1e3a5f",
    "success_bg": "#064e3b",
    "danger_bg": "#450a0a",
    "warning_bg": "#451a03",
}


def apply_dark_theme(app: QApplication) -> None:
    palette = QPalette()
    colors = DARK_PALETTE

    palette.setColor(QPalette.Window, QColor(colors["bg"]))
    palette.setColor(QPalette.WindowText, QColor(colors["text"]))
    palette.setColor(QPalette.Base, QColor(colors["input_bg"]))
    palette.setColor(QPalette.AlternateBase, QColor(colors["panel"]))
    palette.setColor(QPalette.ToolTipBase, QColor(colors["panel"]))
    palette.setColor(QPalette.ToolTipText, QColor(colors["text"]))
    palette.setColor(QPalette.Text, QColor(colors["text"]))
    palette.setColor(QPalette.Button, QColor(colors["panel"]))
    palette.setColor(QPalette.ButtonText, QColor(colors["text"]))
    palette.setColor(QPalette.BrightText, QColor(colors["accent"]))
    palette.setColor(QPalette.Link, QColor(colors["accent"]))
    palette.setColor(QPalette.Highlight, QColor(colors["accent"]))
    palette.setColor(QPalette.HighlightedText, QColor("#000000"))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(colors["muted"]))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(colors["muted"]))

    app.setPalette(palette)

    style = f"""
    QMainWindow, QWidget {{
        background: {colors['bg']};
        color: {colors['text']};
        font-family: 'Segoe UI', 'Inter', sans-serif;
        font-size: 13px;
    }}
    QFrame#panel {{
        background: {colors['panel']};
        border-radius: 10px;
        border: 1px solid {colors['border']};
    }}
    QLabel#heading {{
        font-size: 20px;
        font-weight: 700;
        color: {colors['text']};
    }}
    QLabel#muted {{
        font-size: 13px;
        color: {colors['muted']};
    }}
    QLabel#metric {{
        font-size: 14px;
        font-weight: 600;
        color: {colors['accent']};
    }}
    QLabel#cardTitle {{
        font-size: 16px;
        font-weight: 700;
        color: {colors['text']};
    }}
    QLabel#outputs {{
        font-size: 12px;
        color: {colors['muted']};
        padding: 6px;
        background: {colors['input_bg']};
        border-radius: 6px;
    }}
    QPushButton {{
        background: {colors['panel_alt']};
        color: {colors['text']};
        border: 1px solid {colors['border']};
        border-radius: 6px;
        padding: 8px 18px;
        font-size: 13px;
        font-weight: 600;
    }}
    QPushButton:hover {{
        background: {colors['hover']};
        border: 1px solid {colors['accent']};
    }}
    QPushButton:pressed {{
        background: {colors['accent']};
        color: #000000;
    }}
    QPushButton:disabled {{
        background: {colors['input_bg']};
        color: {colors['muted']};
        border: 1px solid {colors['border']};
    }}
    QLineEdit, QPlainTextEdit {{
        background: {colors['input_bg']};
        color: {colors['text']};
        border: 1px solid {colors['border']};
        border-radius: 6px;
        padding: 6px 10px;
        font-size: 13px;
    }}
    QLineEdit:focus, QPlainTextEdit:focus {{
        border: 1px solid {colors['accent']};
    }}
    QComboBox {{
        background: {colors['input_bg']};
        color: {colors['text']};
        border: 1px solid {colors['border']};
        border-radius: 6px;
        padding: 6px 10px;
        font-size: 13px;
    }}
    QComboBox:hover {{
        border: 1px solid {colors['accent']};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 24px;
    }}
    QComboBox QAbstractItemView {{
        background: {colors['panel']};
        color: {colors['text']};
        selection-background-color: {colors['accent']};
        selection-color: #000000;
        border: 1px solid {colors['border']};
    }}
    QCheckBox {{
        color: {colors['text']};
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 18px;
        height: 18px;
        border-radius: 4px;
        border: 2px solid {colors['muted']};
        background: {colors['input_bg']};
    }}
    QCheckBox::indicator:checked {{
        background: {colors['accent']};
        border: 2px solid {colors['accent']};
    }}
    QTableWidget {{
        background: {colors['panel']};
        color: {colors['text']};
        border: 1px solid {colors['border']};
        border-radius: 8px;
        gridline-color: {colors['border']};
    }}
    QTableWidget::item {{
        padding: 6px 10px;
    }}
    QTableWidget::item:selected {{
        background: {colors['hover']};
        color: {colors['accent']};
    }}
    QHeaderView::section {{
        background: {colors['panel_alt']};
        color: {colors['muted']};
        border: none;
        border-bottom: 2px solid {colors['accent']};
        padding: 8px 10px;
        font-weight: 700;
        font-size: 12px;
        text-transform: uppercase;
    }}
    QListWidget {{
        background: {colors['panel']};
        color: {colors['text']};
        border: none;
        border-radius: 8px;
        padding: 4px;
    }}
    QListWidget::item {{
        padding: 10px 16px;
        border-radius: 6px;
    }}
    QListWidget::item:selected {{
        background: {colors['hover']};
        color: {colors['accent']};
    }}
    QListWidget::item:hover {{
        background: {colors['input_bg']};
    }}
    QSplitter::handle {{
        background: {colors['border']};
        width: 2px;
        margin: 8px 0;
    }}
    QScrollBar:vertical {{
        background: {colors['panel']};
        width: 10px;
        border-radius: 5px;
    }}
    QScrollBar::handle:vertical {{
        background: {colors['border']};
        border-radius: 5px;
        min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {colors['muted']};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QToolBar {{
        background: {colors['panel']};
        border: none;
        border-bottom: 1px solid {colors['border']};
        padding: 4px 8px;
        spacing: 8px;
    }}
    QStatusBar {{
        background: {colors['panel_alt']};
        color: {colors['muted']};
        border-top: 1px solid {colors['border']};
        font-size: 12px;
    }}
    QProgressBar {{
        background: {colors['input_bg']};
        border: none;
        border-radius: 4px;
        height: 6px;
        text-align: center;
    }}
    QProgressBar::chunk {{
        background: {colors['accent']};
        border-radius: 4px;
    }}
    """
    app.setStyleSheet(style)
