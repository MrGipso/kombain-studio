import sys
from pathlib import Path

# Make the project root importable so `import core.stock`, `import
# backend.main`, etc. resolve when running pytest from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_configure(config):
    # pyproject passes --basetemp=.pytest-tmp/pytest. pytest creates the
    # basetemp directory itself but not its parent, which a fresh checkout
    # (CI) does not have, so every tmp_path test would error at setup.
    basetemp = config.option.basetemp
    if basetemp:
        Path(basetemp).parent.mkdir(parents=True, exist_ok=True)
