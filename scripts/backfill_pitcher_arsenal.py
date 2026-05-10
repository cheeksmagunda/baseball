"""Backfill pitcher pitch-arsenal usage % onto player_slate from Baseball
Savant via pybaseball's `statcast_pitcher_pitch_arsenal(arsenal_type='n_')`
leaderboard.

External (no derivations beyond identifying the dominant pitch as the
column with the highest usage value — that's a `max()` over already-
external rates, not a model output).

Per-pitcher columns added by Step 12 schema:
  arsenal_ff_pct, arsenal_si_pct, arsenal_fc_pct, arsenal_sl_pct,
  arsenal_st_pct, arsenal_cu_pct, arsenal_kc_pct, arsenal_ch_pct,
  arsenal_fs_pct, arsenal_kn_pct, arsenal_sv_pct,
  arsenal_dominant_pitch (the abbreviation with the highest %)

Coverage limit: Savant requires `minP` pitches for inclusion in the
leaderboard.  We use `minP=10` to maximize coverage; pitchers with
fewer than 10 season pitches at the time of the slate aren't on the
board and stay NULL.

Usage:
    python scripts/backfill_pitcher_arsenal.py
    python scripts/backfill_pitcher_arsenal.py --force
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-pitcher-arsenal-stub")

from app.core import historical_db  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_pitcher_arsenal")

PITCH_TYPES = ("ff", "si", "fc", "sl", "st", "cu", "kc", "ch", "fs", "kn", "sv")


def fetch_arsenal(season: int, min_pitches: int = 10):
    """Returns {mlb_id: {ff: pct, si: pct, ..., dominant: 'FF'}}."""
    from pybaseball import statcast_pitcher_pitch_arsenal

    df = statcast_pitcher_pitch_arsenal(season, minP=min_pitches, arsenal_type="n_")
    log.info("Savant arsenal leaderboard: %d pitchers (minP=%d)", len(df), min_pitches)

    out: dict[int, dict] = {}
    for _, row in df.iterrows():
        try:
            mid = int(row["pitcher"])
        except (TypeError, ValueError, KeyError):
            continue
        rec: dict[str, float | None] = {}
        max_pct = -1.0
        dominant: str | None = None
        for pt in PITCH_TYPES:
            col = f"n_{pt}"
            v = row.get(col)
            try:
                fv = float(v) if v == v and v is not None else None  # NaN-safe
            except (TypeError, ValueError):
                fv = None
            rec[f"arsenal_{pt}_pct"] = fv
            if fv is not None and fv > max_pct:
                max_pct = fv
                dominant = pt.upper()
        rec["arsenal_dominant_pitch"] = dominant
        out[mid] = rec
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--min-pitches", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    arsenal = fetch_arsenal(args.season, args.min_pitches)

    if args.dry_run:
        log.info("sample: %s", next(iter(arsenal.items()), None))
        return 0

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        # Phase C: primary_position_code lives on player_dim now, not
        # player_slate.  JOIN to filter to pitchers.
        if args.force:
            where = (
                "WHERE ps.mlb_id > 0 AND pd.primary_position_code "
                "IN ('SP', 'RP', 'P', 'TWP')"
            )
        else:
            where = (
                "WHERE ps.mlb_id > 0 AND ps.arsenal_ff_pct IS NULL "
                "AND pd.primary_position_code IN ('SP', 'RP', 'P', 'TWP')"
            )
        cur = conn.execute(
            f"SELECT ps.slate_date, ps.mlb_id FROM player_slate ps "
            f"JOIN player_dim pd ON pd.mlb_id = ps.mlb_id "
            f"{where} ORDER BY ps.slate_date, ps.mlb_id"
        )
        targets = cur.fetchall()
        log.info("pitcher rows to populate: %d", len(targets))

        updates = 0
        missing = 0
        for t in targets:
            rec = arsenal.get(t["mlb_id"])
            if not rec:
                missing += 1
                continue
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"], rec,
            )
            updates += 1
        conn.commit()
        log.info(
            "UPDATE rows: %d (no Savant record / below min-pitches: %d)",
            updates, missing,
        )
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
