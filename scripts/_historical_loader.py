"""Step-4 reader compatibility shim — load historical-corpus rows from
either the SQLite store (default, post-migration) or the CSV/JSON files
(--csv-fallback transitional path, removed in Step 6).

Each loader returns rows in the SAME dict shape that csv.DictReader produces
when reading the corresponding /data/ file, so audit/calibration scripts
swap their CSV-read line for one of these helpers without touching their
logic.

Why this thin layer: data/historical.db is the canonical store after Step 2,
and the CSVs in /data/ are byte-stable derived exports.  Reading from SQLite
is "the right thing" architecturally, but functionally produces identical
results to reading the CSV — the loaders below make that equivalence
explicit and provide a single seam for Step 6 to remove the CSV branch.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "_historical_loader_stub")

DATA_DIR = REPO_ROOT / "data"
HISTORICAL_PLAYERS_CSV = DATA_DIR / "historical_players.csv"
WINNING_DRAFTS_CSV = DATA_DIR / "historical_winning_drafts.csv"
SLATE_RESULTS_JSON = DATA_DIR / "historical_slate_results.json"
PLAYER_GAME_LOGS_CSV = DATA_DIR / "historical_player_game_logs.csv"
HV_STATS_CSV = DATA_DIR / "hv_player_game_stats.csv"


def _load_csv_rows(path: Path) -> list[dict]:
    with path.open() as f:
        return list(csv.DictReader(f))


def _load_json(path: Path):
    with path.open() as f:
        return json.load(f)


def _export_to_tmp_then_load(filename: str, parser):
    """Run the SQLite → CSV/JSON export into a temp dir and parse the
    resulting file.  Equivalent to reading the on-disk export under the
    Step-2 byte-stable contract."""
    import tempfile
    from scripts.export_historical_csvs import export_all
    with tempfile.TemporaryDirectory() as td:
        export_all(out_dir=Path(td))
        return parser(Path(td) / filename)


def load_historical_players(*, source: str = "sqlite") -> list[dict]:
    """Return rows of historical_players.csv as a list of dicts (csv.DictReader-shaped)."""
    if source == "csv":
        return _load_csv_rows(HISTORICAL_PLAYERS_CSV)
    return _export_to_tmp_then_load("historical_players.csv", _load_csv_rows)


def load_winning_drafts(*, source: str = "sqlite") -> list[dict]:
    if source == "csv":
        return _load_csv_rows(WINNING_DRAFTS_CSV)
    return _export_to_tmp_then_load("historical_winning_drafts.csv", _load_csv_rows)


def load_slate_results(*, source: str = "sqlite") -> list[dict]:
    if source == "csv":
        return _load_json(SLATE_RESULTS_JSON)
    return _export_to_tmp_then_load("historical_slate_results.json", _load_json)


def load_player_game_logs(*, source: str = "sqlite") -> list[dict]:
    if source == "csv":
        return _load_csv_rows(PLAYER_GAME_LOGS_CSV)
    return _export_to_tmp_then_load("historical_player_game_logs.csv", _load_csv_rows)


def load_hv_stats(*, source: str = "sqlite") -> list[dict]:
    if source == "csv":
        return _load_csv_rows(HV_STATS_CSV)
    return _export_to_tmp_then_load("hv_player_game_stats.csv", _load_csv_rows)


def add_csv_fallback_flag(parser) -> None:
    """Convenience: every Step-4 reader adds the same --csv-fallback flag.
    Pass the result through `source_from_args(args)` below to get the right
    keyword for the loaders.

    Removed in Step 6 once SQLite is the only source."""
    parser.add_argument(
        "--csv-fallback",
        action="store_true",
        help="Read from /data/ CSVs instead of data/historical.db.  "
             "Transitional flag; removed in Step 6 after parity is verified.",
    )


def source_from_args(args) -> str:
    return "csv" if getattr(args, "csv_fallback", False) else "sqlite"


def env_source() -> str:
    """For readers without an argparse parser (most calibration scripts):
    BO_HISTORICAL_SOURCE=csv falls back to the on-disk CSVs.  Default
    "sqlite" reads through the export shim against data/historical.db.

    Removed in Step 6.
    """
    val = os.environ.get("BO_HISTORICAL_SOURCE", "sqlite").lower()
    if val not in ("csv", "sqlite"):
        raise ValueError(f"BO_HISTORICAL_SOURCE must be 'csv' or 'sqlite', got {val!r}")
    return val
