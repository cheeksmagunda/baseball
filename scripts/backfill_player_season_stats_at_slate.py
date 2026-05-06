"""Backfill season-to-date stats at the time of each historical slate.

Adds five columns to historical reference CSVs:
  * data/historical_players.csv      → ops_at_slate, iso_at_slate (batters)
                                       era_at_slate, whip_at_slate, k9_at_slate (pitchers)
  * data/hv_player_game_stats.csv    → ops_at_slate, iso_at_slate (batters only;
                                       pitcher box scores already cover ip/er/k_pitching)

Why: V13.1 made OPS the anchor sub-signal for `score_offensive_profile`,
and V15.1 needs season-to-date ERA/WHIP/K9 for pitchers to fit the
continuous popularity curve against actual MP-flag outcomes.  To validate
either retrospectively, calibration needs the season aggregates each
player was carrying on each slate date — what the live pipeline would
have seen at T-65.

Calibration-only.  This script only reads + writes /data/ files; it never
touches the live pipeline DB or scoring engine.  Per CLAUDE.md "no
fallbacks" applies to the live T-65 pipeline; backfill scripts are
allowed to leave a row blank with a logged warning when a player has no
record through the slate date (e.g. season-debut starters, mid-day
acquisitions with no team-specific stats yet, or accented names that
fail MLB people-search).

Source: MLB Stats API
    /people/search?names={name}              → resolve mlb_id (with team filter)
    /people/{id}/stats?stats=byDateRange     → season-to-date through endDate
        &endDate=YYYY-MM-DD&group=hitting    → batter OPS + ISO
        &endDate=YYYY-MM-DD&group=pitching   → pitcher ERA + WHIP + K/9
        &season=YYYY

Idempotent: rows where ALL relevant columns are already populated are
skipped (batter rows are skipped if ops_at_slate is filled; pitcher rows
are skipped if era_at_slate is filled).  Re-running fills any rows that
newly become resolvable (e.g. after an accent-normalisation correction
in apply_hv_stats_corrections.py, or after the script is extended to
backfill an additional column on existing rows).

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


def fetch_pitcher_stats_at(
    mlb_id: int, slate_date: str
) -> tuple[float | None, float | None, float | None]:
    """Return (era, whip, k_per_9) season-to-date through `slate_date`.

    Falls back to the prior season aggregates when the current season has
    zero IP — mirroring the live pipeline's
    `fetch_player_season_stats` behaviour for IL-returnees and
    season-debut starters.  This is the player's own previous-season real
    data, not a league-average default.

    Returns (None, None, None) if the player has no pitching record in
    either season.
    """
    season = int(slate_date.split("-", 1)[0])

    def _fetch_for_season(season_year: int, end_date: str) -> tuple[float | None, float | None, float | None, float | None]:
        url = (
            f"{MLB_API}/people/{mlb_id}/stats"
            f"?stats=byDateRange&endDate={end_date}"
            f"&group=pitching&season={season_year}"
        )
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning(
                f"byDateRange pitching fetch failed for mlb_id={mlb_id} "
                f"{end_date} season={season_year}: {e}"
            )
            return (None, None, None, None)

        stats_groups = data.get("stats", []) or []
        for group in stats_groups:
            splits = group.get("splits", []) or []
            if not splits:
                continue
            s = splits[0].get("stat", {}) or {}
            ip = _safe_float(s.get("inningsPitched"))
            era = _safe_float(s.get("era"))
            whip = _safe_float(s.get("whip"))
            k9 = _safe_float(s.get("strikeoutsPer9Inn"))
            return (ip, era, whip, k9)
        return (None, None, None, None)

    ip, era, whip, k9 = _fetch_for_season(season, slate_date)
    if ip is not None and ip > 0 and era is not None:
        return (era, whip, k9)

    # No current-season IP — fall back to prior season's full aggregates.
    # The endDate for the prior season is Dec 31 (capture the full
    # finished season), not the slate date.
    prior_season = season - 1
    prior_end = f"{prior_season}-12-31"
    ip2, era2, whip2, k92 = _fetch_for_season(prior_season, prior_end)
    if ip2 is not None and ip2 > 0 and era2 is not None:
        return (era2, whip2, k92)

    return (None, None, None)


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


def backfill_file(path: Path, dry_run: bool, position_field: str, do_pitchers: bool) -> dict:
    """Backfill at-slate stats columns on `path`.

    Batter rows: ops_at_slate, iso_at_slate.
    Pitcher rows (only if `do_pitchers`): era_at_slate, whip_at_slate, k9_at_slate.

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
    batter_cols = ("ops_at_slate", "iso_at_slate")
    pitcher_cols = ("era_at_slate", "whip_at_slate", "k9_at_slate")
    cols_to_add = batter_cols + (pitcher_cols if do_pitchers else ())
    for col in cols_to_add:
        if col not in new_fields:
            new_fields.append(col)

    batter_todo: list[tuple[int, dict]] = []
    pitcher_todo: list[tuple[int, dict]] = []
    skipped_already_batter = 0
    skipped_already_pitcher = 0
    skipped_pitcher_no_section = 0
    skipped_no_id = 0

    for idx, row in enumerate(rows):
        is_pitcher_row = _is_pitcher_position(row.get(position_field, ""))

        if is_pitcher_row:
            if not do_pitchers:
                skipped_pitcher_no_section += 1
                row.setdefault("ops_at_slate", "")
                row.setdefault("iso_at_slate", "")
                continue
            era_existing = row.get("era_at_slate", "")
            if era_existing not in ("", None):
                skipped_already_pitcher += 1
                continue
            slate_date = row.get("date", "")
            player_name = row.get("player_name", "")
            team = row.get("team") or row.get("team_actual") or ""
            mlb_id = resolve_mlb_id(player_name, team)
            if mlb_id is None:
                skipped_no_id += 1
                log.warning(f"  no mlb_id for pitcher {player_name!r} ({team}) on {slate_date}")
                row.setdefault("ops_at_slate", "")
                row.setdefault("iso_at_slate", "")
                row["era_at_slate"] = ""
                row["whip_at_slate"] = ""
                row["k9_at_slate"] = ""
                continue
            pitcher_todo.append((idx, {"mlb_id": mlb_id, "slate_date": slate_date, "row": row}))
            continue

        # Batter row.
        ops_existing = row.get("ops_at_slate", "")
        iso_existing = row.get("iso_at_slate", "")
        if ops_existing not in ("", None) or iso_existing not in ("", None):
            skipped_already_batter += 1
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
        batter_todo.append((idx, {"mlb_id": mlb_id, "slate_date": slate_date, "row": row}))

    log.info(
        f"{path.name}: batter_todo={len(batter_todo)} pitcher_todo={len(pitcher_todo)} "
        f"skipped_already_batter={skipped_already_batter} "
        f"skipped_already_pitcher={skipped_already_pitcher} "
        f"skipped_pitcher_no_section={skipped_pitcher_no_section} "
        f"unresolved={skipped_no_id}"
    )

    if dry_run:
        log.info(f"{path.name}: --dry-run, no writes")
        return {
            "batter_todo": len(batter_todo),
            "pitcher_todo": len(pitcher_todo),
            "skipped_already_batter": skipped_already_batter,
            "skipped_already_pitcher": skipped_already_pitcher,
            "skipped_pitcher_no_section": skipped_pitcher_no_section,
            "skipped_no_id": skipped_no_id,
        }

    no_record_batter = 0
    no_record_pitcher = 0
    populated_batter = 0
    populated_pitcher = 0
    start = time.time()

    def _work_batter(item):
        idx, ctx = item
        ops, iso = fetch_ops_iso_at(ctx["mlb_id"], ctx["slate_date"])
        return ("batter", idx, ctx["row"], (ops, iso))

    def _work_pitcher(item):
        idx, ctx = item
        era, whip, k9 = fetch_pitcher_stats_at(ctx["mlb_id"], ctx["slate_date"])
        return ("pitcher", idx, ctx["row"], (era, whip, k9))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = []
        for item in batter_todo:
            futures.append(ex.submit(_work_batter, item))
        for item in pitcher_todo:
            futures.append(ex.submit(_work_pitcher, item))
        for fut in as_completed(futures):
            kind, idx, row, payload = fut.result()
            if kind == "batter":
                ops, iso = payload
                if ops is None and iso is None:
                    no_record_batter += 1
                    row["ops_at_slate"] = ""
                    row["iso_at_slate"] = ""
                    log.info(
                        f"  no hitting record: {row.get('player_name')!r} "
                        f"({row.get('team') or row.get('team_actual')}) on {row.get('date')}"
                    )
                else:
                    row["ops_at_slate"] = "" if ops is None else f"{ops:.3f}"
                    row["iso_at_slate"] = "" if iso is None else f"{iso:.3f}"
                    populated_batter += 1
            else:
                era, whip, k9 = payload
                if era is None and whip is None and k9 is None:
                    no_record_pitcher += 1
                    row["era_at_slate"] = ""
                    row["whip_at_slate"] = ""
                    row["k9_at_slate"] = ""
                    log.info(
                        f"  no pitching record: {row.get('player_name')!r} "
                        f"({row.get('team') or row.get('team_actual')}) on {row.get('date')}"
                    )
                else:
                    row["era_at_slate"] = "" if era is None else f"{era:.2f}"
                    row["whip_at_slate"] = "" if whip is None else f"{whip:.2f}"
                    row["k9_at_slate"] = "" if k9 is None else f"{k9:.2f}"
                    populated_pitcher += 1

    elapsed = time.time() - start
    log.info(
        f"{path.name}: populated_batter={populated_batter} no_record_batter={no_record_batter} "
        f"populated_pitcher={populated_pitcher} no_record_pitcher={no_record_pitcher} "
        f"elapsed={elapsed:.1f}s"
    )

    _write_csv(path, rows, new_fields)
    log.info(f"{path.name}: wrote {len(rows)} rows with new columns")

    return {
        "populated_batter": populated_batter,
        "populated_pitcher": populated_pitcher,
        "no_record_batter": no_record_batter,
        "no_record_pitcher": no_record_pitcher,
        "skipped_already_batter": skipped_already_batter,
        "skipped_already_pitcher": skipped_already_pitcher,
        "skipped_pitcher_no_section": skipped_pitcher_no_section,
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

    # Pitcher backfill is meaningful only on historical_players.csv —
    # hv_player_game_stats.csv already has per-game pitcher stats (ip, er,
    # k_pitching) which are the box-score truth, and what we need on the
    # HV-stats file are batter context columns.
    targets = [
        (HISTORICAL_PLAYERS, "position", True),
        (HV_STATS, "position", False),
    ]
    if args.only:
        targets = [(p, f, dp) for (p, f, dp) in targets if p.name == args.only]

    overall: dict[str, dict] = {}
    for path, position_field, do_pitchers in targets:
        log.info(f"=== {path.name} ===")
        overall[path.name] = backfill_file(path, args.dry_run, position_field, do_pitchers)

    log.info("=== summary ===")
    for name, stats in overall.items():
        log.info(f"  {name}: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
