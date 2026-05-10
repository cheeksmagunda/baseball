"""Backfill team standings snapshot at slate_date onto slate_game from MLB
Stats API `/api/v1/standings?leagueId=103,104&season=Y&date=Y-M-D`.

External-only — every column is a verbatim value from the standings endpoint
(games_back, runs_scored, runs_allowed, streak code, division rank,
league rank, home record, away record).

run_differential and winning_pct were dropped in the May 2026 cleanup
sweep — both are pure derivations of the columns above.

Usage:
    python scripts/backfill_standings.py
    python scripts/backfill_standings.py --force
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-standings-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".standings_cache"
MLB_API = "https://statsapi.mlb.com/api/v1"
HTTP_TIMEOUT = 30
MAX_WORKERS = 6

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_standings")


def _safe_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _safe_float(v):
    if v is None or v == "":
        return None
    try:
        # MLB API returns "0.0", "-2.5" or "—" for divisional leaders
        if isinstance(v, str) and v.strip() == "-":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_standings(slate_date: str, season: int = 2026) -> dict:
    """Returns {team_abbr: dict_of_record_fields} from the standings endpoint."""
    cache_file = CACHE_DIR / f"{slate_date}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            pass

    try:
        r = requests.get(
            f"{MLB_API}/standings",
            params={
                "leagueId": "103,104",
                "season": season,
                "date": slate_date,
                "hydrate": "team",
            },
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("standings fetch failed for %s: %s", slate_date, e)
        return {}

    data = r.json()
    out: dict[str, dict] = {}
    for div in data.get("records", []):
        for tr in div.get("teamRecords", []):
            team = (tr.get("team") or {})
            abbr = team.get("abbreviation") or team.get("teamCode") or team.get("fileCode")
            if not abbr:
                continue
            split_records = (tr.get("records") or {}).get("splitRecords") or []
            home_rec = next((s for s in split_records if s.get("type") == "home"), {})
            away_rec = next((s for s in split_records if s.get("type") == "away"), {})
            # run_differential and winning_pct intentionally omitted —
            # both are pure derivations (runs_scored − runs_allowed and
            # W / (W+L)) that were dropped from slate_game in the May 2026
            # cleanup sweep.
            # streak / division_rank / league_rank dropped in May 2026
            # Phase D — autocorrelated with l10_wins / games_back, and
            # too slow-moving to carry independent DFS signal.
            out[abbr.upper()] = {
                "games_back": _safe_float(tr.get("gamesBack")),
                "runs_scored": _safe_int(tr.get("runsScored")),
                "runs_allowed": _safe_int(tr.get("runsAllowed")),
                "home_record": f"{home_rec.get('wins', '?')}-{home_rec.get('losses', '?')}" if home_rec else None,
                "away_record": f"{away_rec.get('wins', '?')}-{away_rec.get('losses', '?')}" if away_rec else None,
            }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(out, indent=2))
    return out


# Map MLB API team abbreviations to our canonical abbrs (handles
# Athletics ATH vs OAK, etc.).  canonicalize_team in app.core.constants
# handles most aliases; we wrap that.
def _canon(abbr: str) -> str:
    from app.core.constants import canonicalize_team
    out = canonicalize_team(abbr)
    if out == "WAS":
        return "WSH"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE 1=1"
        else:
            # Skip-detection: rows that already have runs_scored backfilled
            # don't need a second pass.  (Was `home_team_run_differential
            # IS NULL`; that column dropped in the May 2026 cleanup sweep.)
            where = "WHERE home_team_runs_scored IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, game_pk, game_number, home_team, away_team "
            f"FROM slate_game {where} ORDER BY slate_date, game_pk"
        )
        targets = cur.fetchall()
        log.info("targets: %d", len(targets))
        if not targets:
            log.info("nothing to backfill — re-run with --force to refresh")
            return 0

        unique_dates = sorted({t["slate_date"] for t in targets})
        log.info("unique slate_dates to fetch: %d", len(unique_dates))

        date_to_standings: dict[str, dict] = {}
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(fetch_standings, d, args.season): d for d in unique_dates}
            for fut in as_completed(futures):
                d = futures[fut]
                date_to_standings[d] = fut.result() or {}
        log.info("fetched %d / %d in %.1fs",
                 len(date_to_standings), len(unique_dates), time.time() - t0)

        if args.dry_run:
            sample_d = next(iter(date_to_standings), None)
            if sample_d:
                log.info("sample %s: %s", sample_d, json.dumps(
                    date_to_standings[sample_d], indent=2)[:500])
            return 0

        updates = 0
        skipped = 0
        for t in targets:
            standings = date_to_standings.get(t["slate_date"]) or {}
            home = standings.get(_canon(t["home_team"])) or {}
            away = standings.get(_canon(t["away_team"])) or {}
            if not home and not away:
                skipped += 1
                continue
            update_dict = {}
            for k, v in home.items():
                update_dict[f"home_team_{k}"] = v
            for k, v in away.items():
                update_dict[f"away_team_{k}"] = v
            historical_db.update_slate_game_columns(
                conn, t["slate_date"], t["game_pk"], update_dict,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (skipped %d)", updates, skipped)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        import sys as _sys
        from pathlib import Path as _Path
        _repo = _Path(__file__).resolve().parents[1]
        if str(_repo) not in _sys.path:
            _sys.path.insert(0, str(_repo))
        # Skip the on-disk /data/ export when we're operating against a
        # non-canonical DB (audit reproducibility chain) so the canonical
        # CSV/JSON files in /data/ are not clobbered.
        import os as _os
        if not _os.environ.get('HISTORICAL_DB'):
            from scripts.export_historical_csvs import export_all
            export_all()
    sys.exit(rc)
