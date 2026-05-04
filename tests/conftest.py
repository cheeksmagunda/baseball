"""Pytest bootstrap.

`app.config.Settings.current_season` has no default — production deploys are
required to set `BO_CURRENT_SEASON` explicitly per `CLAUDE.md` (operator picks
the active MLB season year, no auto-derive from `datetime.now().year`).

Tests don't have a real season context, but the module-level `settings = Settings()`
in `app/config.py` runs at import time, so without this hook every test file
fails to collect with a Pydantic validation error.

Set the env var before pytest imports anything from `app.*`. CI already sets
this externally (`.github/workflows/ci.yml`); this file makes `pytest tests/`
work locally without manual export.
"""

import os

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
