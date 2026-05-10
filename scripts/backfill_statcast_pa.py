"""Backfill per-batted-ball Statcast detail into the statcast_pa table.

Tier 3 D12 of the May 2026 cleanup-and-add sweep.

Source: pybaseball.statcast_batter() per (mlb_id, game_date) — pulls
per-PA exit velocity, launch angle, distance, x_woba, pitch type, and
result.

By default operates on HV-flagged player-game pairs only (~750 rows).
Pass --all-games to expand to every player_slate × game (~50× more
rows; estimate ~30k PAs across the corpus).

Calibration unlock: lets the audit ask "did this HV pop come from
quality of contact (sustainable, ~95+ mph EV) or BABIP luck (one-off
sub-90 EV bloop)?"

Cache: scripts/output/.statcast_pa_cache/<game_date>_<mlb_id>.json
(one file per (mlb_id, game_date) — pybaseball internally caches too).

Usage:
    python scripts/backfill_statcast_pa.py
    python scripts/backfill_statcast_pa.py --all-games
"""
from __future__ import annotations

import argparse
import json
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

CACHE_DIR = ROOT / "scripts" / "output" / ".statcast_pa_cache"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_statcast_pa")


def _fetch(mlb_id: int, game_date: str) -> list[dict]:
    cache_file = CACHE_DIR / f"{game_date}_{mlb_id}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            pass
    try:
        from pybaseball import statcast_batter
    except ImportError:
        return []
    try:
        df = statcast_batter(game_date, game_date, mlb_id)
    except Exception as e:
        log.debug("statcast_batter(%s, %s) failed: %s", mlb_id, game_date, e)
        return []
    if df is None or df.empty:
        return []
    rows: list[dict] = []
    # Group rows into PAs via the at_bat_number column (PA index in game).
    if "at_bat_number" in df.columns:
        for ab_num, g in df.groupby("at_bat_number"):
            last = g.iloc[-1]  # final pitch of PA carries the result
            rows.append({
                "pa_index": int(ab_num),
                "exit_velocity_mph": _f(last.get("launch_speed")),
                "launch_angle_deg": _f(last.get("launch_angle")),
                "hit_distance_ft": _f(last.get("hit_distance_sc")),
                "x_woba": _f(last.get("estimated_woba_using_speedangle")),
                "pitch_type": str(last.get("pitch_type") or "") or None,
                "result": str(last.get("events") or "") or None,
            })
    if rows:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(rows))
    return rows


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN → None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all-games", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)

        if args.all_games:
            cur = conn.execute(
                "SELECT slate_date, mlb_id FROM player_slate "
                "WHERE position NOT IN ('P','SP','RP','TWP') "
                "ORDER BY slate_date, mlb_id"
            )
            targets = cur.fetchall()
        else:
            cur = conn.execute(
                "SELECT slate_date, mlb_id FROM label_event "
                "WHERE label_type = 'highest_value' "
                "ORDER BY slate_date, mlb_id"
            )
            targets = cur.fetchall()
        log.info("targets: %d player-game pairs", len(targets))

        if not args.force:
            cur = conn.execute(
                "SELECT DISTINCT slate_date, mlb_id FROM statcast_pa"
            )
            already = {(r["slate_date"], r["mlb_id"]) for r in cur.fetchall()}
            targets = [t for t in targets if (t["slate_date"], t["mlb_id"]) not in already]
            log.info("after skip-already: %d", len(targets))

        observed_at = datetime.now(timezone.utc).isoformat()
        rows_written = 0
        misses = 0
        for t in targets:
            pas = _fetch(t["mlb_id"], t["slate_date"])
            if not pas:
                misses += 1
                continue
            for pa in pas:
                conn.execute(
                    "INSERT OR REPLACE INTO statcast_pa "
                    "(slate_date, mlb_id, game_date, pa_index, "
                    " exit_velocity_mph, launch_angle_deg, hit_distance_ft, "
                    " x_woba, pitch_type, result, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        t["slate_date"], t["mlb_id"], t["slate_date"], pa["pa_index"],
                        pa.get("exit_velocity_mph"), pa.get("launch_angle_deg"),
                        pa.get("hit_distance_ft"), pa.get("x_woba"),
                        pa.get("pitch_type"), pa.get("result"), observed_at,
                    ),
                )
                rows_written += 1
        conn.commit()
        log.info("INSERT statcast_pa rows: %d (no PA data: %d)", rows_written, misses)
        if rows_written == 0 and targets:
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
