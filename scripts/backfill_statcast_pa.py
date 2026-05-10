"""Backfill per-batted-ball Statcast detail into the statcast_pa table.

Tier 3 D12 of the May 2026 cleanup-and-add sweep.

Source: bulk season Statcast pull via pybaseball (shared with
backfill_recent_handedness_splits and backfill_wpa).  Per-PA we record
exit velocity, launch angle, distance, x_woba, pitch type, and result.

By default operates on HV-flagged player-game pairs only (~750 rows).
Pass --all-games to expand to every player_slate × game (~50× more
rows; ~30k PAs across the corpus).

Calibration unlock: lets the audit ask "did this HV pop come from
quality of contact (sustainable, ~95+ mph EV) or BABIP luck (one-off
sub-90 EV bloop)?"

Usage:
    python scripts/backfill_statcast_pa.py
    python scripts/backfill_statcast_pa.py --all-games
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-statcast-pa-stub")

from app.core import historical_db  # noqa: E402
from scripts._statcast_bulk import load_bulk_statcast  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_statcast_pa")


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN → None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all-games", action="store_true")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    try:
        import pandas as pd
    except ImportError:
        log.error("pandas required")
        return 1

    df = load_bulk_statcast(season=args.season)
    if df is None or df.empty:
        log.warning("0 rows written — bulk Statcast unreachable.")
        return 0

    keep_cols = [
        "game_date", "batter", "at_bat_number",
        "launch_speed", "launch_angle", "hit_distance_sc",
        "estimated_woba_using_speedangle", "pitch_type", "events",
    ]
    keep_cols = [c for c in keep_cols if c in df.columns]
    df = df[keep_cols].copy()
    df["game_date"] = pd.to_datetime(df["game_date"]).dt.date.astype(str)

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        if args.all_games:
            cur = conn.execute(
                "SELECT slate_date, mlb_id FROM player_slate "
                "WHERE position NOT IN ('P','SP','RP','TWP') "
                "ORDER BY slate_date, mlb_id"
            )
        else:
            cur = conn.execute(
                "SELECT DISTINCT slate_date, mlb_id FROM label_event "
                "WHERE label_type = 'highest_value' "
                "ORDER BY slate_date, mlb_id"
            )
        targets = cur.fetchall()
        log.info("targets: %d player-game pairs", len(targets))

        if not args.force:
            cur = conn.execute("SELECT DISTINCT slate_date, mlb_id FROM statcast_pa")
            already = {(r["slate_date"], r["mlb_id"]) for r in cur.fetchall()}
            targets = [t for t in targets if (t["slate_date"], t["mlb_id"]) not in already]
            log.info("after skip-already: %d", len(targets))

        # Index events by (batter, game_date)
        events_by_pg = {}
        for (b, d), g in df.groupby(["batter", "game_date"]):
            events_by_pg[(int(b), d)] = g

        observed_at = datetime.now(timezone.utc).isoformat()
        rows_written = 0
        misses = 0
        for t in targets:
            ev = events_by_pg.get((t["mlb_id"], t["slate_date"]))
            if ev is None or ev.empty:
                misses += 1
                continue
            # Group by at_bat_number to get one row per PA, taking the final
            # pitch (it carries the result).
            if "at_bat_number" in ev.columns:
                for ab_num, g in ev.groupby("at_bat_number"):
                    last = g.iloc[-1]
                    conn.execute(
                        "INSERT OR REPLACE INTO statcast_pa "
                        "(slate_date, mlb_id, game_date, pa_index, "
                        " exit_velocity_mph, launch_angle_deg, hit_distance_ft, "
                        " x_woba, pitch_type, result, observed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            t["slate_date"], t["mlb_id"], t["slate_date"],
                            int(ab_num),
                            _f(last.get("launch_speed")),
                            _f(last.get("launch_angle")),
                            _f(last.get("hit_distance_sc")),
                            _f(last.get("estimated_woba_using_speedangle")),
                            str(last.get("pitch_type") or "") or None,
                            str(last.get("events") or "") or None,
                            observed_at,
                        ),
                    )
                    rows_written += 1
        conn.commit()
        log.info("INSERT statcast_pa rows: %d (no PA data: %d)", rows_written, misses)
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
