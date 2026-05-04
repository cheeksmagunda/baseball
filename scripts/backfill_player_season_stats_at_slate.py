"""Backfill season-to-date OPS + ISO at the time of each historical slate.

Adds two columns to two historical reference CSVs:
  * data/historical_players.csv      → ops_at_slate, iso_at_slate
  * data/hv_player_game_stats.csv    → ops_at_slate, iso_at_slate

Why: V13.1 made OPS the anchor sub-signal for `score_offensive_profile`
(40-pt batter trait, was kinematics-only).  To validate the new signal
retrospectively against actual HV outcomes, calibration needs the
season-to-date OPS each player was carrying on each slate date — that's
what the live pipeline would have seen at T-65.

Calibration-only.  This script only reads + writes /data/ files; it never
touches the live pipeline DB or scoring engine.  Per CLAUDE.md "no
fallbacks" applies to the live T-65 pipeline; backfill scripts are
allowed to leave a row blank with a logged warning when a player has no
hitting record through the slate date (e.g. pitchers in
historical_players.csv, season-debut players, or accented names that
fail MLB people-search).

Source: MLB Stats API
    /people/search?names={name}              → resolve mlb_id (with team filter)
    /people/{id}/stats?stats=byDateRange     → season-to-date OPS through endDate
        &endDate=YYYY-MM-DD&group=hitting
        &season=YYYY

Idempotent: rows where the new columns are already populated are skipped.
Re-running fills any rows that newly become resolvable (e.g. after an
accent-normalisation correction in apply_hv_stats_corrections.py).

Usage:
    python scripts/backfill_player_season_stats_at_slate.py
    python scripts/backfill_player_season_stats_at_slate.py --dry-run
    python scripts/backfill_player_season_stats_at_slate.py --only historical_players.csv
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent.parent
HISTORICAL_PLAYERS = ROOT / "data" / "historical_players.csv"
HV_STATS = ROOT / "data" / "hv_player_game_stats.csv"
MLB_API = "https://statsapi.mlb.com/api/v1"
HTTP_TIMEOUT = 20
MAX_WORKERS = 16

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


def _normalize(name: str) -> str:
    """Strip accents, lowercase. Mirrors `app/models/player.py::normalize_name`."""
    nfkd = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).strip().lower()


def _safe_float(v) -> float | None:
    if v in (None, "", "-", ".---"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_id_cache: dict[tuple[str, str], int | None] = {}


def resolve_mlb_id(player_name: str, team: str) -> int | None:
    """Resolve (name, team) → mlb_id via MLB /people/search.

    Returns None when no result matches the team or the search fails.
    Cached for the run.
    """
    key = (_normalize(player_name), (team or "").upper())
    if key in _id_cache:
        return _id_cache[key]

    try:
        url = f"{MLB_API}/people/search?names={quote(player_name)}"
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        people = r.json().get("people", [])
    except Exception as e:
        log.warning(f"people/search failed for {player_name!r}: {e}")
        _id_cache[key] = None
        return None

    if not people:
        _id_cache[key] = None
        return None

    target_team = (team or "").upper()
    target_norm = _normalize(player_name)

    # First pass: exact name normalize match AND team match.
    for p in people:
        full = p.get("fullName", "")
        if _normalize(full) != target_norm:
            continue
        team_obj = p.get("currentTeam") or {}
        team_abbrev = (team_obj.get("abbreviation") or "").upper()
        if target_team and team_abbrev == target_team:
            mlb_id = p.get("id")
            _id_cache[key] = mlb_id
            return mlb_id

    # Second pass: exact name normalize match alone (player may have changed
    # teams since the slate date).  Accept the first hit.
    for p in people:
        full = p.get("fullName", "")
        if _normalize(full) == target_norm:
            mlb_id = p.get("id")
            _id_cache[key] = mlb_id
            return mlb_id

    _id_cache[key] = None
    return None


def fetch_ops_iso_at(mlb_id: int, slate_date: str) -> tuple[float | None, float | None]:
    """Return (ops, iso) season-to-date through `slate_date` (inclusive).

    The `byDateRange` stats split returns hitting aggregates from the start
    of the season through `endDate`. ISO is computed from SLG and AVG when
    both are present. Returns (None, None) if the player has no hitting
    record through that date — the caller logs and skips.
    """
    season = int(slate_date.split("-", 1)[0])
    url = (
        f"{MLB_API}/people/{mlb_id}/stats"
        f"?stats=byDateRange&endDate={slate_date}"
        f"&group=hitting&season={season}"
    )
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"byDateRange fetch failed for mlb_id={mlb_id} {slate_date}: {e}")
        return (None, None)

    stats_groups = data.get("stats", []) or []
    for group in stats_groups:
        splits = group.get("splits", []) or []
        if not splits:
            continue
        s = splits[0].get("stat", {}) or {}
        ops = _safe_float(s.get("ops"))
        avg = _safe_float(s.get("avg"))
        slg = _safe_float(s.get("slg"))
        iso: float | None = None
        if slg is not None and avg is not None:
            iso = round(slg - avg, 3)
        return (ops, iso)
    return (None, None)


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


def backfill_file(path: Path, dry_run: bool, position_field: str) -> dict:
    """Backfill `ops_at_slate` and `iso_at_slate` columns on `path`.

    `position_field` names the column to read for the position string —
    differs between the two CSVs ("position" in both, but kept explicit
    so this works if a future ingest renames it).
    """
    if not path.exists():
        log.error(f"{path} does not exist")
        return {"status": "missing"}

    rows, fieldnames = _read_csv(path)
    log.info(f"{path.name}: {len(rows)} rows")

    # Add new columns to the header if absent.
    new_fields = list(fieldnames)
    for col in ("ops_at_slate", "iso_at_slate"):
        if col not in new_fields:
            new_fields.append(col)

    # Group rows that need fetching by (mlb_id, date) — many slate-mates
    # share a player but we only need one byDateRange call per (id, date)
    # pair.  However different rows on the same date but different teams
    # may legitimately resolve to different mlb_ids if the player was
    # traded — keep keying by (name, team, date) for resolve, and
    # (mlb_id, date) for fetch.

    todo: list[tuple[int, dict]] = []
    skipped_already = 0
    skipped_pitcher = 0
    skipped_no_id = 0

    for idx, row in enumerate(rows):
        ops_existing = row.get("ops_at_slate", "")
        iso_existing = row.get("iso_at_slate", "")
        if ops_existing not in ("", None) or iso_existing not in ("", None):
            skipped_already += 1
            continue

        if _is_pitcher_position(row.get(position_field, "")):
            skipped_pitcher += 1
            row["ops_at_slate"] = ""
            row["iso_at_slate"] = ""
            continue

        slate_date = row.get("date", "")
        player_name = row.get("player_name", "")
        team = row.get("team") or row.get("team_actual") or ""
        mlb_id = resolve_mlb_id(player_name, team)
        if mlb_id is None:
            skipped_no_id += 1
            log.warning(f"  no mlb_id for {player_name!r} ({team}) on {slate_date}")
            row["ops_at_slate"] = ""
            row["iso_at_slate"] = ""
            continue
        todo.append((idx, {"mlb_id": mlb_id, "slate_date": slate_date, "row": row}))

    log.info(
        f"{path.name}: skipped already-populated={skipped_already} "
        f"pitchers={skipped_pitcher} unresolved={skipped_no_id} todo={len(todo)}"
    )

    if dry_run:
        log.info(f"{path.name}: --dry-run, no writes")
        return {
            "todo": len(todo),
            "skipped_already": skipped_already,
            "skipped_pitcher": skipped_pitcher,
            "skipped_no_id": skipped_no_id,
        }

    # Concurrent fetch with bounded workers.
    no_record = 0
    populated = 0
    start = time.time()

    def _work(item):
        idx, ctx = item
        ops, iso = fetch_ops_iso_at(ctx["mlb_id"], ctx["slate_date"])
        return idx, ctx["row"], ops, iso

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(_work, item) for item in todo]
        for fut in as_completed(futures):
            idx, row, ops, iso = fut.result()
            if ops is None and iso is None:
                no_record += 1
                row["ops_at_slate"] = ""
                row["iso_at_slate"] = ""
                log.info(
                    f"  no hitting record: {row.get('player_name')!r} "
                    f"({row.get('team') or row.get('team_actual')}) on {row.get('date')}"
                )
            else:
                row["ops_at_slate"] = "" if ops is None else f"{ops:.3f}"
                row["iso_at_slate"] = "" if iso is None else f"{iso:.3f}"
                populated += 1

    elapsed = time.time() - start
    log.info(
        f"{path.name}: populated={populated} no_record={no_record} "
        f"elapsed={elapsed:.1f}s"
    )

    _write_csv(path, rows, new_fields)
    log.info(f"{path.name}: wrote {len(rows)} rows with new columns")

    return {
        "populated": populated,
        "no_record": no_record,
        "skipped_already": skipped_already,
        "skipped_pitcher": skipped_pitcher,
        "skipped_no_id": skipped_no_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Report only — don't write changes")
    parser.add_argument(
        "--only",
        choices=["historical_players.csv", "hv_player_game_stats.csv"],
        help="Limit to one file",
    )
    args = parser.parse_args()

    targets = [
        (HISTORICAL_PLAYERS, "position"),
        (HV_STATS, "position"),
    ]
    if args.only:
        targets = [(p, f) for (p, f) in targets if p.name == args.only]

    overall: dict[str, dict] = {}
    for path, position_field in targets:
        log.info(f"=== {path.name} ===")
        overall[path.name] = backfill_file(path, args.dry_run, position_field)

    log.info("=== summary ===")
    for name, stats in overall.items():
        log.info(f"  {name}: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
