#!/bin/bash
set -euo pipefail

# Only run in Claude Code cloud sessions
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
    exit 0
fi

# ── Git credential helper (HTTPS auth via GITHUB_PAT) ─────────────────────
if [ -n "${GITHUB_PAT:-}" ]; then
    git config --global credential.helper \
        '!f() { echo username=x-access-token; echo "password=$GITHUB_PAT"; }; f'
    git config --global credential.useHttpPath true

    # Export standard token names for tools that expect them
    echo "export GH_TOKEN=\"${GITHUB_PAT}\"" >> "$CLAUDE_ENV_FILE"
    echo "export GITHUB_TOKEN=\"${GITHUB_PAT}\"" >> "$CLAUDE_ENV_FILE"
    echo "==> git credentials configured via GITHUB_PAT ✓"
else
    echo "WARNING: GITHUB_PAT not set — git push/pull over HTTPS will require manual auth."
fi

# ── Python dependencies ────────────────────────────────────────────────────
PYTHON=$(command -v python3)
echo "==> Python: $("$PYTHON" --version)"
"$PYTHON" -m pip install --quiet -e ".[dev]"
echo "==> Python deps installed ✓"

# ── Frontend dependencies ──────────────────────────────────────────────────
if [ -f "frontend/package.json" ]; then
    echo "==> Installing frontend deps..."
    (cd frontend && npm install --silent)
    echo "==> Frontend deps installed ✓"
fi

# ── Runtime scaffolding ────────────────────────────────────────────────────
mkdir -p db
echo "==> db/ ready ✓"

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "==> .env created from .env.example (set BO_CURRENT_SEASON, BO_ODDS_API_KEY, BO_REDIS_URL) ✓"
fi

echo "==> Session start complete ✓"
