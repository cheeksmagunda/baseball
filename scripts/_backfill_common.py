"""Shared helpers for the backfill scripts.

Consolidates the patterns that were previously duplicated across
~30 scripts:

  - `_safe_int` / `_safe_float` parsing helpers (was in 13 scripts)
  - Boilerplate environment setup (`bootstrap()`)
  - Footer that re-exports CSVs after a successful run (`finalize()`)
  - Savant CSV reader that strips the UTF-8 BOM Savant emits

Importing from this module is preferred over copy-pasting these
helpers into each new backfill — keeps a single source of truth and
makes a future change (e.g. tightening the CSV parser) one-edit-fits-all.

Public surface:

    from scripts._backfill_common import (
        bootstrap, finalize, safe_int, safe_float, read_savant_csv,
    )

    bootstrap("backfill-my-thing-stub")
    from app.core import historical_db  # AFTER bootstrap so env vars set

    def main() -> int:
        ...
        return 0

    if __name__ == "__main__":
        sys.exit(finalize(main()))

`bootstrap()` sets the BO_CURRENT_SEASON / BO_ODDS_API_KEY env-var
defaults and inserts the repo root onto `sys.path`.  `finalize()`
re-runs the on-disk CSV exports if the script returned 0 AND the
`HISTORICAL_DB` env var is unset (the audit-reproducibility hook).
"""
from __future__ import annotations

import csv
import io
import os
import sys
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]


def bootstrap(odds_key_stub: str = "backfill-stub") -> None:
    """Set up env-var defaults and sys.path.  Idempotent.

    Call this before importing `app.core.historical_db` (or any other
    `app/` module) — `app.core.constants` validates BO_ODDS_API_KEY at
    import time.
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ.setdefault("BO_CURRENT_SEASON", "2026")
    os.environ.setdefault("BO_ODDS_API_KEY", odds_key_stub)


def finalize(rc: int) -> int:
    """Re-export CSVs on success unless we're targeting a non-canonical DB.

    Returns the same `rc` for `sys.exit(finalize(rc))` ergonomics.
    """
    if rc == 0 and not os.environ.get("HISTORICAL_DB"):
        # Lazy import so this module can be loaded by scripts that don't
        # re-export (e.g. dry-run audits).
        try:
            from scripts.export_historical_csvs import export_all
            export_all()
        except Exception as e:
            # Log to stderr but don't fail the run — exports are derived
            # artefacts and a missing one shouldn't void a successful
            # SQLite write.
            print(f"warning: export_all() raised {e}", file=sys.stderr)
    return rc


def safe_int(v) -> int | None:
    """Coerce to int; return None for empty/None/non-numeric."""
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def safe_float(v) -> float | None:
    """Coerce to float; return None for empty/None/non-numeric/NaN."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN check


def read_savant_csv(text: str) -> Iterator[dict]:
    """Parse a Savant CSV response.

    Savant emits a UTF-8 BOM that breaks DictReader's column-name match
    if not stripped — this is the single most common backfill bug we
    keep re-introducing.  `read_savant_csv()` strips it and yields one
    dict per row.
    """
    return csv.DictReader(io.StringIO(text.lstrip("﻿")))
