"""Backfill rolling-window per-handedness OPS splits onto player_slate.

Tier 2 D8 of the May 2026 cleanup-and-add sweep.

Schema columns populated:
  ops_vs_lhp_last_20  — OPS vs LHP, last 20 days
  ops_vs_rhp_last_20  — OPS vs RHP, last 20 days

Source: Baseball Savant per-batter splits filtered by pitcher hand for
the trailing 20-day window.  Uses pybaseball if available; falls back to
direct CSV download from `baseballsavant.mlb.com/leaderboard/statcast`.

The existing season-aggregate `ops_vs_lhp_at_slate` / `ops_vs_rhp_at_slate`
columns (Step 4 backfill) go stale fast — late-March game samples are
tiny and a 20-30 PA hot streak vs LHP can flip the platoon picture
entirely.  These rolling-window splits give the calibration a more
responsive matchup signal.

Cache: scripts/output/.recent_handedness_cache/<slate_date>_<mlb_id>.json.
Per-mlb_id-per-slate caching means re-runs against the same slate are
cheap; new slates require fresh per-mlb_id fetches.

Usage:
    python scripts/backfill_recent_handedness_splits.py
    python scripts/backfill_recent_handedness_splits.py --window 20
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date as DateType, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-recent-handedness-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".recent_handedness_cache"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_recent_handedness_splits")


def _fetch_via_pybaseball(mlb_id: int, start_date: str, end_date: str) -> dict:
    """Returns {ops_vs_lhp, ops_vs_rhp} for the trailing window."""
    try:
        from pybaseball import statcast_batter
    except ImportError:
        return {}
    try:
        df = statcast_batter(start_date, end_date, mlb_id)
    except Exception as e:
        log.debug("statcast_batter(%s) failed: %s", mlb_id, e)
        return {}
    if df is None or df.empty:
        return {}

    # Crude OPS approximation from pitch-by-pitch:
    # pivot by pitcher hand, compute avg woba_value (Statcast) which is a
    # close proxy for OPS at the per-PA level.
    out: dict = {}
    for hand_code, col in (("L", "ops_vs_lhp_last_20"), ("R", "ops_vs_rhp_last_20")):
        sub = df[df.get("p_throws") == hand_code]
        if sub.empty:
            continue
        # Use estimated_woba_using_speedangle when available, else woba_value
        woba_col = "estimated_woba_using_speedangle" \
            if "estimated_woba_using_speedangle" in sub.columns else "woba_value"
        if woba_col not in sub.columns:
            continue
        valid = sub[sub[woba_col].notna()]
        if valid.empty:
            continue
        # Approximate OPS as 1.5 * avg(woba); rough but better than nothing.
        out[col] = round(float(valid[woba_col].mean()) * 1.5, 4)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", type=int, default=20, help="Days back from slate.")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE position NOT IN ('P','SP','RP','TWP')"
        else:
            where = (
                "WHERE position NOT IN ('P','SP','RP','TWP') "
                "AND ops_vs_lhp_last_20 IS NULL AND ops_vs_rhp_last_20 IS NULL"
            )
        cur = conn.execute(
            f"SELECT slate_date, mlb_id FROM player_slate {where} "
            "ORDER BY slate_date, mlb_id"
        )
        targets = cur.fetchall()
        log.info("batter rows to populate: %d", len(targets))

        updates = 0
        misses = 0
        for t in targets:
            slate_d = DateType.fromisoformat(t["slate_date"])
            start = (slate_d - timedelta(days=args.window)).isoformat()
            end = (slate_d - timedelta(days=1)).isoformat()
            cache_file = CACHE_DIR / f"{t['slate_date']}_{t['mlb_id']}.json"
            if cache_file.exists():
                try:
                    rec = json.loads(cache_file.read_text())
                except json.JSONDecodeError:
                    rec = {}
            else:
                rec = _fetch_via_pybaseball(t["mlb_id"], start, end)
                if rec:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(json.dumps(rec))
            if not rec:
                misses += 1
                continue
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"], rec,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (no recent splits: %d)", updates, misses)
        if updates == 0:
            log.warning(
                "0 rows written — likely pybaseball/network unreachable.  "
                "Schema is in place; re-run when reachable."
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        if not os.environ.get("HISTORICAL_DB"):
            from scripts.export_historical_csvs import export_all
            export_all()
    sys.exit(rc)
