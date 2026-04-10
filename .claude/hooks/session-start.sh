#!/bin/bash
set -euo pipefail

# Only run in remote (Claude Code on the web) environments
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

echo "==> Installing Python dependencies..."
pip install --quiet -e ".[dev]"

echo "==> Creating db/ directory if missing..."
mkdir -p db

echo "==> Creating .env if missing..."
if [ ! -f ".env" ]; then
  cp .env.example .env
fi

echo "==> Setting PYTHONPATH..."
echo 'export PYTHONPATH="$CLAUDE_PROJECT_DIR"' >> "$CLAUDE_ENV_FILE"

echo "==> Session start complete."
