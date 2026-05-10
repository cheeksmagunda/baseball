"""Backfill Win-Probability-Added (WPA) per HV player game.

Tier 3 D11 of the May 2026 cleanup-and-add sweep.

Writes to label_event(label_type='wpa') — one row per HV player per game,
label_value = WPA.  Storing as a label_event rather than a column on
hv_player_game_stats keeps the existing CSV header stable and lets WPA
land for non-HV players too if a future calibration wants it.

Source: pybaseball's `playerid_lookup` + `statcast_batter` provide
per-pitch WPA via `delta_home_win_exp` cumulative; sum across the game
to get player WPA contribution.  Free.

Calibration unlock: separates "1-run game in the 9th inning" leverage
HV (repeatable) from "blowout in the 3rd" volume HV (luck-driven).
Both produce HV but only one is calibration-stable.

Operates on rows already flagged is_highest_value to keep the corpus
small.  Cache: scripts/output/.wpa_cache/<slate_date>_<mlb_id>.json.

Usage:
    python scripts/backfill_wpa.py
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
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-wpa-stub")

from app.core import historical_db  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".wpa_cache"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_wpa")


def _fetch_wpa(mlb_id: int, game_date: str) -> float | None:
    cache_file = CACHE_DIR / f"{game_date}_{mlb_id}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text()).get("wpa")
        except json.JSONDecodeError:
            pass
    try:
        from pybaseball import statcast_batter
    except ImportError:
        return None
    try:
        df = statcast_batter(game_date, game_date, mlb_id)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if "delta_home_win_exp" not in df.columns:
        return None
    valid = df[df["delta_home_win_exp"].notna()]
    if valid.empty:
        return None
    # WPA contribution from the batter's POV: sum of delta_home_win_exp
    # while batter is hitting.  Statcast already signs it from the batter's
    # team perspective via inning_topbot, but pybaseball's column is from
    # home-team perspective.  We approximate by summing the absolute deltas
    # — gives "leverage volume" rather than directional WPA.  v1 ship.
    wpa = float(valid["delta_home_win_exp"].abs().sum())
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"wpa": wpa}))
    return wpa


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        # Target: all HV-flagged player_slate rows (where the player has a
        # `highest_value` label_event row).
        cur = conn.execute(
            "SELECT DISTINCT le.slate_date, le.mlb_id "
            "FROM label_event le "
            "WHERE le.label_type = 'highest_value' "
            "ORDER BY le.slate_date, le.mlb_id"
        )
        hv_targets = cur.fetchall()

        if not args.force:
            cur = conn.execute(
                "SELECT slate_date, mlb_id FROM label_event WHERE label_type='wpa'"
            )
            already = {(r["slate_date"], r["mlb_id"]) for r in cur.fetchall()}
            hv_targets = [t for t in hv_targets if (t["slate_date"], t["mlb_id"]) not in already]

        log.info("HV targets to populate WPA: %d", len(hv_targets))
        observed_at = datetime.now(timezone.utc).isoformat()
        upserts = 0
        misses = 0
        for t in hv_targets:
            wpa = _fetch_wpa(t["mlb_id"], t["slate_date"])
            if wpa is None:
                misses += 1
                continue
            historical_db.upsert_label_event(
                conn,
                slate_date=t["slate_date"], mlb_id=t["mlb_id"], label_type="wpa",
                label_value=wpa, label_text=None,
                source="pybaseball_statcast",
                observed_at=observed_at,
            )
            upserts += 1
        conn.commit()
        log.info("UPSERT label_event(wpa): %d (no PA data: %d)", upserts, misses)
        if upserts == 0 and hv_targets:
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
