#!/usr/bin/env bash
# Updates a git checkout: pulls the latest code of the current branch, syncs
# Python packages and rebuilds the web UI if its sources changed. Settings and
# job history live in ~/.local/share/PipelineStudio and are not touched.
# Close the application before running this.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found. Install it: https://docs.astral.sh/uv/" >&2
    exit 1
fi

uv run --no-sync python tools/self_update.py

echo
echo "Updating Python packages..."
# --inexact keeps packages installed by hand (for example the Qt backend of
# the native window on Linux).
uv sync --inexact --extra desktop-app

echo
uv run python tools/frontend_build.py --if-stale

echo
echo "Done. Start Kombain Studio as usual."
