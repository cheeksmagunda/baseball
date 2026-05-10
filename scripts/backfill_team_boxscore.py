"""Backfill per-team post-game box-score totals onto slate_game.

Reuses the gumbo-feed cache from Step 9.  Pulls per-team batting line
(boxscore.teams.{home|away}.teamStats.batting).  All pure post-game externals.

Schema columns populated:
  home/away_team_hits, doubles, triples, hr,
  home/away_team_walks, strikeouts, left_on_base,
  home/away_team_stolen_bases, errors

Note: innings_played dropped from the schema (~95% are 9; if extras-game
sensitivity matters, add a binary went_extras column instead).
home/away_team_runs dropped (duplicates home_score / away_score).

Usage:
    python scripts/backfill_team_boxscore.py
    python scripts/backfill_team_boxscore.py --force
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-team-boxscore-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".game_externals_cache"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_team_boxscore")


def _safe_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _team_batting(team_box: dict) -> dict:
    # `runs` intentionally omitted — duplicates home_score / away_score
    # and was dropped from slate_game in the May 2026 cleanup sweep.
    bat = (team_box.get("teamStats") or {}).get("batting") or {}
    return {
        "hits":         _safe_int(bat.get("hits")),
        "doubles":      _safe_int(bat.get("doubles")),
        "triples":      _safe_int(bat.get("triples")),
        "hr":           _safe_int(bat.get("homeRuns")),
        "walks":        _safe_int(bat.get("baseOnBalls")),
        "strikeouts":   _safe_int(bat.get("strikeOuts")),
        "left_on_base": _safe_int(bat.get("leftOnBase")),
        "stolen_bases": _safe_int(bat.get("stolenBases")),
    }


def _team_errors(team_box: dict) -> int | None:
    fld = (team_box.get("teamStats") or {}).get("fielding") or {}
    return _safe_int(fld.get("errors"))


def extract(payload: dict) -> dict:
    if not payload:
        return {}
    bx = (payload.get("liveData") or {}).get("boxscore") or {}
    teams = bx.get("teams") or {}
    out: dict = {}
    for side in ("home", "away"):
        bat = _team_batting(teams.get(side, {}) or {})
        for k, v in bat.items():
            out[f"{side}_team_{k}"] = v
        out[f"{side}_team_errors"] = _team_errors(teams.get(side, {}) or {})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        if args.force:
            where = "WHERE game_pk IS NOT NULL"
        else:
            # Skip-detection: rows that already have hits backfilled don't
            # need a second pass.  (Was `innings_played IS NULL`; that column
            # dropped in the May 2026 cleanup sweep.)
            where = "WHERE game_pk IS NOT NULL AND home_team_hits IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, game_pk, game_number FROM slate_game "
            f"{where} ORDER BY slate_date, game_pk"
        )
        targets = cur.fetchall()
        log.info("targets: %d", len(targets))
        if not targets:
            log.info("nothing to backfill — re-run with --force to refresh")
            return 0

        unique_pks = sorted({t["game_pk"] for t in targets})
        log.info("unique game_pks (cache hits expected): %d", len(unique_pks))

        payloads: dict[int, dict] = {}
        for pk in unique_pks:
            cf = CACHE_DIR / f"{pk}.json"
            if cf.exists():
                try:
                    payloads[pk] = json.loads(cf.read_text())
                except json.JSONDecodeError:
                    pass
        log.info("loaded %d / %d cached gumbo payloads", len(payloads), len(unique_pks))

        if args.dry_run:
            sample_pk = next(iter(payloads), None)
            if sample_pk:
                log.info("sample: %s", json.dumps(extract(payloads[sample_pk]), indent=2))
            return 0

        updates = 0
        for t in targets:
            payload = payloads.get(t["game_pk"])
            if not payload:
                continue
            ext = extract(payload)
            if not ext:
                continue
            historical_db.update_slate_game_columns(
                conn, t["slate_date"], t["game_pk"], ext,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d", updates)
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
