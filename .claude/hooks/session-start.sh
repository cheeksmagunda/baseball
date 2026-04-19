#!/bin/bash
set -euo pipefail

# ── 1. Verify Python version ────────────────────────────────────────────────
REQUIRED_PYTHON_MINOR=11
PYTHON=$(command -v python3 || command -v python || echo "")
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python not found. Install Python 3.11+ and re-run."
    exit 1
fi

MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo 0)
MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo 0)
if [ "$MAJOR" -ne 3 ] || [ "$MINOR" -lt "$REQUIRED_PYTHON_MINOR" ]; then
    echo "ERROR: Python 3.${REQUIRED_PYTHON_MINOR}+ required. Found ${MAJOR}.${MINOR}."
    exit 1
fi
echo "==> Python $("$PYTHON" --version 2>&1) ✓"

# ── 2. Validate required files ──────────────────────────────────────────────
if [ ! -f ".env.example" ]; then
    echo "ERROR: .env.example not found. This file is required to set up .env."
    exit 1
fi

# ── 3. Install Python dependencies ──────────────────────────────────────────
echo "==> Installing Python dependencies..."
if ! pip install --quiet -e ".[dev]"; then
    echo "ERROR: pip install failed. Check dependencies in pyproject.toml."
    exit 1
fi

# ── 4. Create db/ directory ────────────────────────────────────────────────
echo "==> Creating db/ directory if missing..."
mkdir -p db

# ── 5. Create .env if missing ──────────────────────────────────────────────
if [ ! -f ".env" ]; then
    echo "==> Creating .env from .env.example..."
    cp .env.example .env
    echo "    → BO_CURRENT_SEASON and BO_ODDS_API_KEY must be set in .env"
else
    echo "==> .env already exists ✓"
fi

# ── 6. Set PYTHONPATH ──────────────────────────────────────────────────────
echo "==> Setting PYTHONPATH..."
echo 'export PYTHONPATH="$CLAUDE_PROJECT_DIR"' >> "$CLAUDE_ENV_FILE"

# ── 7. Load project documentation ──────────────────────────────────────────
echo "==> Loading project documentation..."
echo ""
echo "--- CLAUDE.md (full) ---"
cat "$CLAUDE_PROJECT_DIR/CLAUDE.md" 2>/dev/null || echo "(CLAUDE.md not found)"
echo ""
echo "--- README.md (full) ---"
cat "$CLAUDE_PROJECT_DIR/README.md" 2>/dev/null || echo "(README.md not found)"
echo ""

# ── 8. Print next steps ────────────────────────────────────────────────────
echo "==> Session start complete ✓"
echo ""
echo "Next steps:"
echo "  1. Check .env: BO_CURRENT_SEASON=2026, BO_ODDS_API_KEY set"
echo "  2. If CSV data in data/ changed: rm db/ben_oracle.db && python -m app.seed"
echo "  3. Start the app: python run.py"
echo ""

# ── 9. Historical data ingest reference ────────────────────────────────────
echo "📖 For detailed data ingest workflow, see CLAUDE.md section: 'Ingesting New Slate Data'"
echo ""
