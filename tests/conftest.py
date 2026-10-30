import sys
from pathlib import Path

# Make the project root importable so `import core.stock`, `import
# backend.main`, etc. resolve when running pytest from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
