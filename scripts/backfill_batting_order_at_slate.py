"""Backfill the actual batting-order slot each historical-corpus batter occupied
on their slate date.

Adds one column to historical_players.csv:
  * batting_order_at_slate  (1-9, integer; blank for non-starters,
                             pinch-hits, defensive subs, and pitchers)

Why
---
The live T-65 pipeline reads `SlatePlayer.batting_order` (RotoWire-projected)
and uses it for the V12 batter env Group B "lineup_position" sub-signal — top
of the order gets a PA-volume premium.  The historical CSVs have no batting
order column at all today; calibration sweeps that try to validate the
batting-order signal can't, because the data isn't there.

RotoWire has no public archive, but the official MLB box score (posted
post-game) carries the actual lineup card per team.  This is strictly
post-hoc — pinch-hits and double-switches are baked in — so it's the
"closest-to-RotoWire" historical surrogate.  Documented as ACTUAL (not
EXPECTED) in column docstring; calibration scripts that compare RotoWire-
signal correlations against this column should treat the gap explicitly.

Source
------
MLB Stats API `/game/{game_pk}/boxscore`.  `game_pk` is already populated on
every game in `data/historical_slate_results.json`, so this script reads
that file as the index — no schedule re-fetch needed.

The boxscore response contains per-team `battingOrder`: an ordered list of
MLB IDs (slot 1 first).  We take the first 9 entries; later entries are
pinch-runners / mid-game defensive subs that didn't start the game.

Idempotent: rows where batting_order_at_slate is already populated are
skipped.  Pitcher rows are skipped.

Calibration-only.  This script only reads + writes /data/ files; never
touches the live pipeline DB or scoring engine.

Usage
-----
    python scripts/backfill_batting_order_at_slate.py
    python scripts/backfill_batting_order_at_slate.py --dry-run
    python scripts/backfill_batting_order_at_slate.py --force
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
HISTORICAL_PLAYERS = ROOT / "data" / "historical_players.csv"
SLATE_RESULTS = ROOT / "data" / "historical_slate_results.json"
MLB_API = "https://statsapi.mlb.com/api/v1"
HTTP_TIMEOUT = 20
MAX_WORKERS = 16

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

sys.path.insert(0, str(ROOT))
from scripts.backfill_player_season_stats_at_slate import resolve_mlb_id  # noqa: E402

# Same canonicalisation table as backfill_v10_8_signals — historical "AZ" maps
# to the canonical "ARI" the rest of the codebase uses.
TEAM_ABBR_ALIASES = {
    "KCR": "KC", "CHW": "CWS", "AZ": "ARI", "WSN": "WSH",
    "TBR": "TB", "SDP": "SD", "SFG": "SF", "OAK": "ATH",
}


def _canon_team(team: str) -> str:
    return TEAM_ABBR_ALIASES.get(team.strip().upper(), team.strip().upper())


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


def build_game_index(slates: list[dict]) -> dict[tuple[str, str], int]:
    """Map (date, canonical_team) → game_pk."""
    out: dict[tuple[str, str], int] = {}
    for s in slates:
        d = s.get("date")
        if not d:
            continue
        for g in s.get("games", []):
            pk = g.get("game_pk")
            if not pk:
                continue
            for side in ("home", "away"):
                team = _canon_team(g.get(side) or "")
                if team:
                    out[(d, team)] = pk
    return out


def fetch_batting_order(game_pk: int) -> dict[int, int]:
    """Return {mlb_id: slot} for all 18 starters across both teams."""
    url = f"{MLB_API}/game/{game_pk}/boxscore"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"boxscore fetch failed for game_pk={game_pk}: {e}")
        return {}

    out: dict[int, int] = {}
    for side in ("home", "away"):
        team = data.get("teams", {}).get(side, {}) or {}
        bo = team.get("battingOrder", []) or []
        for slot_idx, pid in enumerate(bo[:9], start=1):
            try:
                out[int(pid)] = slot_idx
            except (TypeError, ValueError):
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill batting-order slot onto historical_players.csv")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--force", action="store_true",
        help="Re-fetch even when batting_order_at_slate is already populated",
    )
    args = ap.parse_args()

    slates = json.load(open(SLATE_RESULTS))
    log.info(f"{SLATE_RESULTS.name}: {len(slates)} slates")

    rows, fieldnames = _read_csv(HISTORICAL_PLAYERS)
    log.info(f"{HISTORICAL_PLAYERS.name}: {len(rows)} rows")

    new_col = "batting_order_at_slate"
    new_fields = list(fieldnames)
    if new_col not in new_fields:
        new_fields.append(new_col)
    for row in rows:
        row.setdefault(new_col, "")

    game_index = build_game_index(slates)
    log.info(f"Game index: {len(game_index)} (date, team) → game_pk pairs")

    # Group rows by game_pk so each box score is fetched once.
    todo: dict[int, list[tuple[int, int]]] = {}  # game_pk → [(row_idx, mlb_id), ...]
    skipped_pitcher = 0
    skipped_already = 0
    skipped_no_id = 0
    skipped_no_game = 0

    for idx, row in enumerate(rows):
        if _is_pitcher_position(row.get("position", "")):
            skipped_pitcher += 1
            continue
        if not args.force and row.get(new_col):
            skipped_already += 1
            continue

        slate_date = row.get("date", "")
        team = _canon_team(row.get("team") or "")
        game_pk = game_index.get((slate_date, team))
        if not game_pk:
            skipped_no_game += 1
            continue

        player_name = row.get("player_name", "")
        mlb_id = resolve_mlb_id(player_name, team)
        if mlb_id is None:
            skipped_no_id += 1
            log.warning(f"  no mlb_id for {player_name!r} ({team}) on {slate_date}")
            continue
        todo.setdefault(game_pk, []).append((idx, mlb_id))

    log.info(
        f"Games to fetch: {len(todo)} | rows to populate: {sum(len(v) for v in todo.values())} | "
        f"skipped_pitcher={skipped_pitcher} skipped_already={skipped_already} "
        f"unresolved_id={skipped_no_id} unresolved_game={skipped_no_game}"
    )

    if args.dry_run:
        log.info("--dry-run, no fetch")
        return 0

    populated = 0
    not_starter = 0
    t0 = time.time()

    def _work(item: tuple[int, list[tuple[int, int]]]):
        game_pk, players = item
        slot_map = fetch_batting_order(game_pk)
        return (players, slot_map)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(_work, item) for item in todo.items()]
        for fut in as_completed(futures):
            players, slot_map = fut.result()
            for idx, mlb_id in players:
                slot = slot_map.get(mlb_id)
                if slot is None:
                    not_starter += 1
                    rows[idx][new_col] = ""
                else:
                    rows[idx][new_col] = str(slot)
                    populated += 1

    elapsed = time.time() - t0
    log.info(
        f"populated={populated} not_starter={not_starter} "
        f"skipped_pitcher={skipped_pitcher} skipped_already={skipped_already} "
        f"unresolved_id={skipped_no_id} unresolved_game={skipped_no_game} "
        f"elapsed={elapsed:.1f}s"
    )

    _write_csv(HISTORICAL_PLAYERS, rows, new_fields)
    log.info(f"Wrote {HISTORICAL_PLAYERS} with batting_order_at_slate column")

    batter_rows = [r for r in rows if not _is_pitcher_position(r.get("position", ""))]
    populated_rows = sum(1 for r in batter_rows if r.get(new_col))
    cov = 100.0 * populated_rows / len(batter_rows) if batter_rows else 0.0
    log.info(f"Batter-row coverage: {populated_rows}/{len(batter_rows)} = {cov:.1f}%")
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0:
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
