"""Backfill z_contact_pct on player_slate from the bulk Statcast pull.

Phase C add (May 2026).  z_contact_pct = batter contact rate on swings
at pitches in the strike zone.  Industry-standard plate-discipline
metric; the V14 leverage and live env stack will fold this in once
populated.

Defining contact:
  - Pitches in zone: Statcast `zone` ∈ {1..9}.  Out-of-zone is 11-14.
  - Swings: any of {hit_into_play, foul, foul_tip, swinging_strike,
    swinging_strike_blocked, foul_bunt, missed_bunt, bunt_foul_tip}.
  - Contact: swings that did NOT result in a swinging strike (i.e.
    everything except swinging_strike + swinging_strike_blocked).

  z_contact_pct = (in-zone swings that made contact) / (in-zone swings)

The Savant `percentile-rankings` endpoint we already use covers BB%,
K%, O-Swing% (as chase_percent), and SwStr% (whiff_percent) but NOT
Z-Contact%.  FanGraphs has it but is Cloudflare-protected.  Computing
from the bulk Statcast pull avoids both gaps.

Cache: scripts/output/.recent_handedness_cache/statcast_<season>.parquet
(shared with all other Tier-3 backfills).

Usage:
    python scripts/backfill_z_contact_pct.py
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-z-contact-stub")

from app.core import historical_db  # noqa: E402
from scripts._statcast_bulk import load_bulk_statcast  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_z_contact_pct")

SWING_DESCRIPTIONS = {
    "hit_into_play",
    "foul",
    "foul_tip",
    "swinging_strike",
    "swinging_strike_blocked",
    "foul_bunt",
    "missed_bunt",
    "bunt_foul_tip",
}
WHIFF_DESCRIPTIONS = {
    "swinging_strike",
    "swinging_strike_blocked",
    "missed_bunt",
}
IN_ZONE_VALUES = {1, 2, 3, 4, 5, 6, 7, 8, 9}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if importlib.util.find_spec("pandas") is None:
        log.error("pandas required")
        return 1

    df = load_bulk_statcast(season=args.season)
    if df is None or df.empty:
        log.warning("0 rows written — bulk Statcast unreachable.")
        return 0
    if "zone" not in df.columns or "description" not in df.columns:
        log.warning("Statcast frame missing zone/description columns")
        return 0

    sub = df[["batter", "zone", "description"]].copy()
    sub = sub.dropna(subset=["batter"])
    sub["batter"] = sub["batter"].astype(int)
    sub["in_zone"] = sub["zone"].isin(IN_ZONE_VALUES)
    sub["is_swing"] = sub["description"].isin(SWING_DESCRIPTIONS)
    sub["is_whiff"] = sub["description"].isin(WHIFF_DESCRIPTIONS)

    in_zone_swings = sub[sub["in_zone"] & sub["is_swing"]]
    grouped = in_zone_swings.groupby("batter").agg(
        z_swings=("is_swing", "sum"),
        z_whiffs=("is_whiff", "sum"),
    )
    grouped["z_contact_pct"] = (
        (grouped["z_swings"] - grouped["z_whiffs"]) / grouped["z_swings"] * 100.0
    )
    # Require at least 30 in-zone swings for the rate to be meaningful
    grouped = grouped[grouped["z_swings"] >= 30]
    z_contact_lookup = {int(b): float(round(v, 2)) for b, v in grouped["z_contact_pct"].items()}
    log.info("z_contact_pct computed for %d batters", len(z_contact_lookup))

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE position NOT IN ('P','SP','RP','TWP')"
        else:
            where = (
                "WHERE position NOT IN ('P','SP','RP','TWP') "
                "AND z_contact_pct IS NULL"
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
            v = z_contact_lookup.get(t["mlb_id"])
            if v is None:
                misses += 1
                continue
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"], {"z_contact_pct": v},
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (no z-contact data: %d)", updates, misses)
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
