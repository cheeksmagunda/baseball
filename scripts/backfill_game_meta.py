"""Backfill mound-visits + ABS-challenges per game onto slate_game.

Reuses the gumbo-feed cache from Step 9 — no new API calls.

Schema columns populated:
  home/away_mound_visits_used     — count of mound visits used this game
  home/away_abs_challenges_used   — total ABS challenges used (success + failed)
  home/away_abs_challenges_won    — successful ABS challenges (overturned calls)

Usage:
    python scripts/backfill_game_meta.py
    python scripts/backfill_game_meta.py --force
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
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-game-meta-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".game_externals_cache"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_game_meta")


def _safe_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def extract(payload: dict) -> dict:
    if not payload:
        return {}
    gd = payload.get("gameData") or {}
    visits = gd.get("moundVisits") or {}
    challenges = gd.get("absChallenges") or {}

    home_v = visits.get("home") or {}
    away_v = visits.get("away") or {}
    home_c = challenges.get("home") or {}
    away_c = challenges.get("away") or {}

    return {
        "home_mound_visits_used": _safe_int(home_v.get("used")),
        "away_mound_visits_used": _safe_int(away_v.get("used")),
        "home_abs_challenges_used": (
            (_safe_int(home_c.get("usedSuccessful")) or 0)
            + (_safe_int(home_c.get("usedFailed")) or 0)
        ) if (home_c.get("usedSuccessful") is not None or home_c.get("usedFailed") is not None) else None,
        "home_abs_challenges_won": _safe_int(home_c.get("usedSuccessful")),
        "away_abs_challenges_used": (
            (_safe_int(away_c.get("usedSuccessful")) or 0)
            + (_safe_int(away_c.get("usedFailed")) or 0)
        ) if (away_c.get("usedSuccessful") is not None or away_c.get("usedFailed") is not None) else None,
        "away_abs_challenges_won": _safe_int(away_c.get("usedSuccessful")),
    }


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
            where = "WHERE game_pk IS NOT NULL AND home_mound_visits_used IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, game_pk, game_number FROM slate_game "
            f"{where} ORDER BY slate_date, game_pk"
        )
        targets = cur.fetchall()
        log.info("targets: %d", len(targets))
        if not targets:
            return 0
        unique_pks = sorted({t["game_pk"] for t in targets})
        payloads: dict[int, dict] = {}
        for pk in unique_pks:
            cf = CACHE_DIR / f"{pk}.json"
            if cf.exists():
                try:
                    payloads[pk] = json.loads(cf.read_text())
                except json.JSONDecodeError:
                    pass

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
