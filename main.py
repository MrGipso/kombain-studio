#!/usr/bin/env python3
"""Canonical Комбайн Studio entry point.

The maintained interface is the FastAPI/React application shown in a native
window by :mod:`launch`.  Keeping ``python main.py`` as a thin compatibility
entry point prevents the retired PySide workflow from accidentally launching
with different settings and orchestration rules.
"""

from __future__ import annotations

import sys

from launch import main


if __name__ == "__main__":
    sys.exit(main())
