#!/usr/bin/env bash
# setup_dev.sh — Bootstrap a local dev environment for ben-oracle
set -euo pipefail

VENV_DIR=".venv"
REQUIRED_PYTHON_MINOR=11

# ── 1. Find a Python 3.11+ interpreter ───────────────────────────────────────
find_python() {
    for cmd in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            minor=$("$cmd" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo 0)
            major=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo 0)
            if [ "$major" -eq 3 ] && [ "$minor" -ge "$REQUIRED_PYTHON_MINOR" ]; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    echo ""
}

PYTHON=$(find_python)
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.${REQUIRED_PYTHON_MINOR}+ not found. Install it and re-run."
    exit 1
fi

echo "Using $($PYTHON --version)"

# ── 2. Create virtual environment ────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment in $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "Virtual environment already exists at $VENV_DIR — skipping creation."
fi

# Activate
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ── 3. Install dependencies ───────────────────────────────────────────────────
echo "Upgrading pip ..."
pip install --quiet --upgrade pip

echo "Installing project + dev extras ..."
pip install --quiet -e ".[dev]"

# ── 4. Create db/ directory ───────────────────────────────────────────────────
if [ ! -d "db" ]; then
    echo "Creating db/ directory ..."
    mkdir -p db
fi

# ── 5. Create .env if missing ─────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    echo "Creating .env from .env.example ..."
    cp .env.example .env
    echo "  → Edit .env to customize settings (DB URL, Redis, etc.)"
else
    echo ".env already exists — skipping."
fi

# ── 6. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "Setup complete. To start:"
echo "  source $VENV_DIR/bin/activate"
echo "  python run.py"
