#!/usr/bin/env python3
"""Canonical Комбайн Studio entry point.

The maintained interface is the FastAPI/React application shown in a native
window by :mod:`launch`.  ``python main.py`` is kept as a thin compatibility
alias for it.
"""

from __future__ import annotations

import sys

from launch import main


if __name__ == "__main__":
    sys.exit(main())
