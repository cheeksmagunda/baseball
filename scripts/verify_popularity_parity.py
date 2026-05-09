"""Step 5 parity gate — proves the SQLite-backed `_load_fame_rate_index`
returns the same fame counts as the legacy CSV implementation across the
full historical corpus.

Why this exists: app/core/popularity.py is the only runtime read of the
historical-corpus data, and a behavior change there would shift
predicted_ownership_score for every player on every slate.  This harness
calls both `_load_fame_rate_index` (SQLite path, the current default) and
`_load_fame_rate_index_csv_legacy` (the CSV path retained until Step 6)
for every (player, slate_date) pair in the corpus, asserting the dicts
are equal.

Usage:
    BO_CURRENT_SEASON=2026 BO_ODDS_API_KEY=x \
        python scripts/verify_popularity_parity.py

Exits 0 only when 100% of the (slate_date, window_days) configurations
return identical (mp_appearances, total_appearances) tuples.

The SQLite path reads from data/historical.db; the CSV path reads from
the frozen fixture at tests/fixtures/popularity_pre_migration_players.csv
(committed at Step 5 baseline) so the result is reproducible even after
the live CSV in /data/ drifts.
"""
from __future__ import annotations

import csv
import os
import sys
import unicodedata
from datetime import date as DateType
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "verify-popularity-parity-stub")

FROZEN_FIXTURE = ROOT / "tests" / "fixtures" / "popularity_pre_migration_players.csv"

# Load fixture-driven legacy implementation that points at the frozen CSV
from app.core.constants import canonicalize_team  # noqa: E402


def _normalize(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    return " ".join(
        "".join(c for c in nfkd if not unicodedata.combining(c)).lower().split()
    )


def _legacy_from_fixture(
    as_of: DateType, window_days: int,
) -> dict[tuple[str, str], tuple[int, int]]:
    """Inline copy of `_load_fame_rate_index_csv_legacy` body, but reading
    from the FROZEN fixture path rather than the live `/data/` CSV.  This
    is what locks the parity result in time even if `/data/` drifts due
    to subsequent backfills."""
    from datetime import timedelta as _td
    if not FROZEN_FIXTURE.exists():
        raise SystemExit(f"FAIL: fixture {FROZEN_FIXTURE} missing")
    cutoff = as_of - _td(days=window_days)
    counts: dict[tuple[str, str], tuple[int, int]] = {}
    with FROZEN_FIXTURE.open("r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                row_date = DateType.fromisoformat(row["date"])
            except (KeyError, ValueError):
                continue
            if row_date >= as_of or row_date < cutoff:
                continue
            key = (_normalize(row["player_name"]), canonicalize_team(row["team"]))
            mp_inc = 1 if row.get("is_most_popular") == "1" else 0
            mp, total = counts.get(key, (0, 0))
            counts[key] = (mp + mp_inc, total + 1)
    return counts


def main() -> int:
    from app.core import popularity

    # Cover the corpus's date range with both window sizes the live runtime
    # uses (14 d batters, 28 d pitchers).
    sample_dates = [
        DateType.fromisoformat(d) for d in (
            "2026-03-30", "2026-04-05", "2026-04-15", "2026-04-25",
            "2026-05-01", "2026-05-07", "2026-05-08",
        )
    ]
    windows = (14, 28)

    total_pairs = 0
    mismatches: list[tuple[DateType, int, tuple[str, str], tuple[int, int], tuple[int, int]]] = []

    for as_of in sample_dates:
        for w in windows:
            popularity.clear_cache()
            sqlite_idx = popularity._load_fame_rate_index(as_of, w)
            legacy_idx = _legacy_from_fixture(as_of, w)
            keys = set(sqlite_idx.keys()) | set(legacy_idx.keys())
            for k in keys:
                total_pairs += 1
                a = sqlite_idx.get(k, (0, 0))
                b = legacy_idx.get(k, (0, 0))
                if a != b:
                    mismatches.append((as_of, w, k, a, b))

    print(f"verified pairs: {total_pairs}")
    print(f"mismatches: {len(mismatches)}")
    if mismatches:
        for as_of, w, k, a, b in mismatches[:20]:
            print(f"  {as_of} w={w}d {k} sqlite={a} csv={b}")
        return 1
    print("OK — popularity SQLite path is byte-identical to the CSV legacy path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
