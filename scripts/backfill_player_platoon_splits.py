"""Backfill batter platoon splits (OPS vs LHP / vs RHP) onto historical_players.csv.

Adds two columns:
  * ops_vs_lhp_at_slate
  * ops_vs_rhp_at_slate

These are inputs the live trait scorer reads (`PlayerStats.ops_vs_lhp` /
`ops_vs_rhp`) for the matchup_quality batter sub-signal — see CLAUDE.md
V10.6 (matchup_quality is a four-sub-signal blend including 30% on
batter-vs-handedness OPS split).

Why "_at_slate" naming despite the season-total caveat
------------------------------------------------------
The MLB Stats API exposes `stats=statSplits&sitCodes=vl,vr` but does NOT
honour `endDate` on that endpoint — every slate date returns the same
season-total split.  This is the same shape the live runtime gets at
T-65 (it pulls current season-to-date stats), and the source of truth
in PlayerStats does not carry a point-in-time history.  By the time we
backfill (May 2026) the season is mid-flight, so the value approximates
what each slate's T-65 would have seen — exact for the most recent
slates, slightly stale for late-March games.  This is the same
"season-aggregate not point-in-time" caveat called out for V10.8 xStats
and framing.

Idempotent: rows where BOTH columns are already populated are skipped.
Pitcher rows are skipped entirely.

Source: MLB Stats API
    /people/{id}/stats?stats=statSplits&group=hitting&season=YYYY&sitCodes=vl
    /people/{id}/stats?stats=statSplits&group=hitting&season=YYYY&sitCodes=vr
(Two separate calls — comma-separated sitCodes silently collapse to the
season total in the API.)

Calibration-only.  This script only reads + writes /data/ files; it never
touches the live pipeline DB or scoring engine.

Usage
-----
    python scripts/backfill_player_platoon_splits.py
    python scripts/backfill_player_platoon_splits.py --dry-run
    python scripts/backfill_player_platoon_splits.py --force
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
HISTORICAL_PLAYERS = ROOT / "data" / "historical_players.csv"
MLB_API = "https://statsapi.mlb.com/api/v1"
HTTP_TIMEOUT = 20
MAX_WORKERS = 16

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# Reuse the proven (name, team) → mlb_id resolver from the season-stats backfill.
sys.path.insert(0, str(ROOT))
from scripts.backfill_player_season_stats_at_slate import (  # noqa: E402
    resolve_mlb_id,
    _safe_float,
)


def _is_pitcher_position(pos: str) -> bool:
    return (pos or "").upper() in {"P", "SP", "RP"}


def _read_csv(path: Path) -> tuple[list[dict], list[str]]:
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def fetch_platoon_split(mlb_id: int, season: int, sit_code: str) -> float | None:
    """Return season-total OPS for the given platoon split.  None if no record."""
    url = (
        f"{MLB_API}/people/{mlb_id}/stats"
        f"?stats=statSplits&group=hitting&season={season}&sitCodes={sit_code}"
    )
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"statSplits fetch failed for mlb_id={mlb_id} sit={sit_code}: {e}")
        return None

    for group in data.get("stats", []) or []:
        for split in group.get("splits", []) or []:
            sc = split.get("split", {}).get("code", "")
            if sc != sit_code:
                continue
            return _safe_float(split.get("stat", {}).get("ops", ""))
    return None


def fetch_both_splits(mlb_id: int, season: int) -> tuple[float | None, float | None]:
    """Return (ops_vs_lhp, ops_vs_rhp) season-total for the player."""
    return (
        fetch_platoon_split(mlb_id, season, "vl"),
        fetch_platoon_split(mlb_id, season, "vr"),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill platoon splits onto historical_players.csv")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--force", action="store_true",
        help="Re-fetch even when ops_vs_{lhp,rhp}_at_slate are already populated",
    )
    args = ap.parse_args()

    rows, fieldnames = _read_csv(HISTORICAL_PLAYERS)
    log.info(f"{HISTORICAL_PLAYERS.name}: {len(rows)} rows")

    new_cols = ("ops_vs_lhp_at_slate", "ops_vs_rhp_at_slate")
    new_fields = list(fieldnames)
    for col in new_cols:
        if col not in new_fields:
            new_fields.append(col)
    for row in rows:
        for col in new_cols:
            row.setdefault(col, "")

    # Group rows by (mlb_id, season).  Splits are season-total so we can
    # cache the fetch and apply to every slate this player appears on.
    todo: dict[tuple[int, int], list[int]] = {}
    skipped_pitcher = 0
    skipped_already = 0
    skipped_no_id = 0

    for idx, row in enumerate(rows):
        if _is_pitcher_position(row.get("position", "")):
            skipped_pitcher += 1
            continue
        if not args.force and row.get("ops_vs_lhp_at_slate") and row.get("ops_vs_rhp_at_slate"):
            skipped_already += 1
            continue

        slate_date = row.get("date", "")
        season = int(slate_date.split("-", 1)[0]) if slate_date else 2026
        player_name = row.get("player_name", "")
        team = row.get("team") or ""
        mlb_id = resolve_mlb_id(player_name, team)
        if mlb_id is None:
            skipped_no_id += 1
            log.warning(f"  no mlb_id for {player_name!r} ({team}) on {slate_date}")
            continue
        todo.setdefault((mlb_id, season), []).append(idx)

    log.info(
        f"Players to fetch: {len(todo)} | rows to populate: {sum(len(v) for v in todo.values())} | "
        f"skipped_pitcher={skipped_pitcher} skipped_already={skipped_already} unresolved={skipped_no_id}"
    )

    if args.dry_run:
        log.info("--dry-run, no fetch")
        return 0

    populated = 0
    no_record = 0
    t0 = time.time()

    def _work(item: tuple[tuple[int, int], list[int]]):
        (mlb_id, season), idxs = item
        ops_vl, ops_vr = fetch_both_splits(mlb_id, season)
        return (idxs, ops_vl, ops_vr)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(_work, item) for item in todo.items()]
        for fut in as_completed(futures):
            idxs, ops_vl, ops_vr = fut.result()
            if ops_vl is None and ops_vr is None:
                no_record += 1
                for idx in idxs:
                    rows[idx]["ops_vs_lhp_at_slate"] = ""
                    rows[idx]["ops_vs_rhp_at_slate"] = ""
                continue
            for idx in idxs:
                rows[idx]["ops_vs_lhp_at_slate"] = "" if ops_vl is None else f"{ops_vl:.3f}"
                rows[idx]["ops_vs_rhp_at_slate"] = "" if ops_vr is None else f"{ops_vr:.3f}"
            populated += len(idxs)

    elapsed = time.time() - t0
    log.info(
        f"populated={populated} no_record_players={no_record} "
        f"skipped_pitcher={skipped_pitcher} skipped_already={skipped_already} "
        f"unresolved={skipped_no_id} elapsed={elapsed:.1f}s"
    )

    _write_csv(HISTORICAL_PLAYERS, rows, new_fields)
    log.info(f"Wrote {HISTORICAL_PLAYERS} with platoon-split columns")

    # Coverage check
    batter_rows = [r for r in rows if not _is_pitcher_position(r.get("position", ""))]
    populated_rows = sum(
        1 for r in batter_rows
        if r.get("ops_vs_lhp_at_slate") or r.get("ops_vs_rhp_at_slate")
    )
    cov = 100.0 * populated_rows / len(batter_rows) if batter_rows else 0.0
    log.info(f"Batter-row coverage: {populated_rows}/{len(batter_rows)} = {cov:.1f}%")
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        # Step 3 hook: re-ingest CSVs into data/historical.db so downstream
        # readers (Step 4) see the new values.  Cheap (~1s) and idempotent.
        # Ensure repo root on sys.path so app.core.historical_db imports
        # work regardless of how the backfill was invoked.
        import sys as _sys
        from pathlib import Path as _Path
        _repo = _Path(__file__).resolve().parents[1]
        if str(_repo) not in _sys.path:
            _sys.path.insert(0, str(_repo))
        from app.core import historical_db
        historical_db.rebuild_from_csvs_and_export()
    sys.exit(rc)
