#!/usr/bin/env python3
"""
Pipeline Studio — Universal content production pipeline.
"""

import sys
import os
from pathlib import Path


def main():
    os.chdir(Path(__file__).resolve().parent)

    # Discover and register plugins before UI starts
    from core.registry import discover_and_register
    discover_and_register()

    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "PySide6 is required. Install it with:\n"
            "  pip install PySide6"
        )
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setApplicationName("Pipeline Studio")

    from ui.theme import apply_dark_theme
    apply_dark_theme(app)

    from ui.app import MainWindow
    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
