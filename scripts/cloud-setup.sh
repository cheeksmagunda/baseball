#!/bin/bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════
# Ben Oracle — Claude Code Cloud Environment Setup
#
# THIS FILE IS A REFERENCE COPY. The canonical runtime lives in the
# Claude Code cloud environment "Setup script" field (Settings → Environment).
# Paste this file's contents back into that field if the cloud config is
# lost or reset. It does NOT auto-run from the repo — the cloud harness
# invokes its own copy once per container provision, before Claude starts.
#
# Runs once when the cloud container is provisioned, before Claude Code starts.
# The container itself is the isolation boundary — no venv required.
#
# Principles:
#   - No fallbacks. Every failure is loud and exits non-zero.
#   - pip work goes through `python -m pip` so the active Python and its pip
#     stay bound together.
#   - Asserts required files + secrets up front.
#   - Does NOT seed the DB or run migrations — the FastAPI lifespan does that
#     when the app boots, which matches prod (Dockerfile) behavior.
# ═══════════════════════════════════════════════════════════════════════════

# ── 1. Resolve Python binary ────────────────────────────────────────────────
REQUIRED_PYTHON_MINOR=11

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH."
    exit 1
fi
PYTHON=$(command -v python3)

echo "==> Python binary: $PYTHON"
"$PYTHON" --version

MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")
if [ "$MAJOR" -ne 3 ] || [ "$MINOR" -lt "$REQUIRED_PYTHON_MINOR" ]; then
    echo "ERROR: Python 3.${REQUIRED_PYTHON_MINOR}+ required. Found ${MAJOR}.${MINOR}."
    exit 1
fi

# ── 2. Verify pip is bound to this interpreter ─────────────────────────────
"$PYTHON" -m pip --version

# ── 3. Verify npm + gh are available ───────────────────────────────────────
if ! command -v npm >/dev/null 2>&1; then
    echo "ERROR: npm not found on PATH. Required for frontend setup."
    exit 1
fi
if ! command -v gh >/dev/null 2>&1; then
    echo "ERROR: gh CLI not found on PATH. Required for GitHub auth."
    exit 1
fi
echo "==> npm: $(npm --version) ✓"
echo "==> gh:  $(gh --version | head -1) ✓"

# ── 4. GitHub authentication (GITHUB_PAT → git + gh CLI) ───────────────────
# Faster than the GitHub MCP for read-heavy ops.
# GITHUB_PAT must be set as an env secret in Claude Code cloud settings.
if [ -z "${GITHUB_PAT:-}" ]; then
    echo "ERROR: GITHUB_PAT is not set in the container environment."
    echo "       Add it as an env secret in Claude Code cloud settings."
    exit 1
fi

# Git: credential helper reads PAT from env at runtime.
# No token written to disk — stays in env only.
git config --global credential.helper '!f() { echo username=x-access-token; echo "password=$GITHUB_PAT"; }; f'
git config --global credential.useHttpPath true
echo "==> git credential helper configured (token stays in env) ✓"

# gh CLI: non-interactive auth using the PAT.
echo "$GITHUB_PAT" | gh auth login --with-token --hostname github.com
gh auth status
echo "==> gh CLI authenticated ✓"

# Alias GITHUB_PAT → GH_TOKEN / GITHUB_TOKEN for tools that expect the
# standard env var names. Persisted for future interactive shells.
{
    echo ""
    echo "# Ben Oracle — GitHub tooling aliases"
    echo 'export GH_TOKEN="${GITHUB_PAT:-}"'
    echo 'export GITHUB_TOKEN="${GITHUB_PAT:-}"'
} >> "$HOME/.bashrc"
export GH_TOKEN="$GITHUB_PAT"
export GITHUB_TOKEN="$GITHUB_PAT"
echo "==> GH_TOKEN / GITHUB_TOKEN exported + persisted to ~/.bashrc ✓"

# ── 5. Validate required files ──────────────────────────────────────────────
REQUIRED_FILES=(
    .env.example
    pyproject.toml
    run.py
    CLAUDE.md
    README.md
    Dockerfile
    frontend/package.json
    frontend/package-lock.json
)
for required in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$required" ]; then
        echo "ERROR: $required not found. Cannot proceed."
        exit 1
    fi
done
echo "==> All required files present ✓"

# ── 6. Upgrade pip ─────────────────────────────────────────────────────────
echo "==> Upgrading pip..."
"$PYTHON" -m pip install --upgrade pip

# ── 7. Install Ben Oracle (editable) + dev extras ──────────────────────────
#   - Editable: code changes take effect without reinstall
#   - [dev] brings in pytest, pytest-asyncio, ruff
#   - Prod (Dockerfile) uses `pip install .` without dev extras — intentional
echo "==> Installing Ben Oracle (editable) + dev extras..."
"$PYTHON" -m pip install -e ".[dev]"

# ── 8. Smoke-test backend imports ──────────────────────────────────────────
echo "==> Verifying app.* modules import..."
"$PYTHON" -c "import app; import app.main; import app.services.filter_strategy; import app.services.scoring_engine"

# ── 9. Install frontend dependencies (reproducible via package-lock.json) ──
echo "==> Installing frontend dependencies (npm ci)..."
(cd frontend && npm ci)

# ── 10. Create db/ directory for SQLite (matches Dockerfile behavior) ──────
mkdir -p db
echo "==> db/ ready ✓"

# ── 11. Create .env from .env.example if missing ───────────────────────────
#   The app will fail on startup if BO_CURRENT_SEASON / BO_ODDS_API_KEY /
#   BO_REDIS_URL aren't set. Cloud env vars populate them at runtime;
#   .env is a local-dev convenience.
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "==> .env created from .env.example ✓"
    echo "    → Verify BO_CURRENT_SEASON, BO_ODDS_API_KEY, BO_REDIS_URL"
else
    echo "==> .env already exists ✓"
fi

# ── 12. Print tooling versions for the session transcript ──────────────────
echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "Tooling"
echo "════════════════════════════════════════════════════════════════════"
echo "python:  $("$PYTHON" --version)"
echo "pip:     $("$PYTHON" -m pip --version)"
echo "pytest:  $("$PYTHON" -m pytest --version 2>&1 | head -1)"
echo "ruff:    $("$PYTHON" -m ruff --version)"
echo "node:    $(node --version)"
echo "npm:     $(npm --version)"
echo "gh:      $(gh --version | head -1)"

# ── 13. Load project documentation into the session transcript ─────────────
echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "CLAUDE.md"
echo "════════════════════════════════════════════════════════════════════"
cat CLAUDE.md
echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "README.md"
echo "════════════════════════════════════════════════════════════════════"
cat README.md
echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "frontend/AGENTS.md (Next.js 16 — training-data drift warning)"
echo "════════════════════════════════════════════════════════════════════"
cat frontend/AGENTS.md
echo ""

# ── 14. Next steps ─────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════════════════"
echo "Session start complete ✓"
echo "════════════════════════════════════════════════════════════════════"
echo ""
echo "GitHub: gh + git are authenticated via GITHUB_PAT."
echo "  gh pr list / gh pr view / gh issue list   # faster than the GH MCP"
echo "  git push / git pull                       # HTTPS auth via credential helper"
echo ""
echo "Dev commands:"
echo "  python run.py                  # start backend (seeds DB on first boot via lifespan)"
echo "  cd frontend && npm run dev     # start frontend (Next.js 16)"
echo "  pytest                         # run test suite"
echo "  ruff check .                   # lint"
echo ""
echo "Data ingest (manual):"
echo "  python scripts/scrape_realsports_daily.py --date YYYY-MM-DD  # append to data/"
echo "  See CLAUDE.md → 'Ingesting New Slate Data' for full workflow."
echo ""
echo "Prod parity: Dockerfile uses Python 3.12 + runtime deps only."
echo "Dev env:     Python $("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')") + dev extras."
