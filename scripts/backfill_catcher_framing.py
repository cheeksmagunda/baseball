"""Backfill per-catcher framing onto player_slate.{framing_runs,framing_strike_rate}.

Tier 1 D2 of the May 2026 cleanup-and-add sweep.

Source: Baseball Savant catcher-framing leaderboard
(https://baseballsavant.mlb.com/leaderboard/catcher-framing) — the embedded
JSON of the same page Step 9's team-aggregate framing pulled from, but
unaggregated per-catcher.

Why this matters: the team-aggregate framing column is wrong when the
team's elite framer isn't catching tonight.  V10.8 wired team framing
into score_pitcher_k_rate; this lets future calibration switch to the
actual catcher's framing keyed by slate_game.{home,away}_catcher_id.

Idempotent; cache per (mlb_id, season) at scripts/output/.catcher_framing_cache/.
Re-runs are cheap.

Usage:
    python scripts/backfill_catcher_framing.py
    python scripts/backfill_catcher_framing.py --season 2026
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "backfill-catcher-framing-stub")

from app.core import historical_db  # noqa: E402
from scripts._backfill_common import safe_float as _safe_float  # noqa: E402

CACHE_DIR = ROOT / "scripts" / "output" / ".catcher_framing_cache"
HTTP_TIMEOUT = 30

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_catcher_framing")


def _fetch_leaderboard(season: int) -> dict[int, dict]:
    """{mlb_id: {framing_runs, framing_strike_rate}} for the season."""
    cache_file = CACHE_DIR / f"{season}.json"
    if cache_file.exists():
        try:
            return {int(k): v for k, v in json.loads(cache_file.read_text()).items()}
        except json.JSONDecodeError:
            pass
    # Savant CSV endpoint — verified 2026 schema.  Columns: id, name, pitches,
    # rv_tot (total run value from framing), pct_tot (overall strike rate above
    # average), then per-zone breakdowns (rv_11..rv_19 / pct_11..pct_19).
    url = (
        "https://baseballsavant.mlb.com/leaderboard/catcher-framing"
        f"?year={season}&team=&min=q&sort=4,1&csv=true"
    )
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            log.warning("framing leaderboard fetch returned %s", r.status_code)
            return {}
        # Savant CSVs are emitted with a UTF-8 BOM that breaks DictReader's
        # column name match if not stripped.
        reader = csv.DictReader(io.StringIO(r.text.lstrip("﻿")))
        out: dict[int, dict] = {}
        for row in reader:
            try:
                pid = int(row.get("id") or row.get("player_id") or row.get("mlb_id") or 0)
            except ValueError:
                continue
            if pid <= 0:
                continue
            out[pid] = {
                "framing_runs": _safe_float(row.get("rv_tot") or row.get("runs_extra_strikes")),
                "framing_strike_rate": _safe_float(row.get("pct_tot") or row.get("strike_rate_above_average")),
            }
    except Exception as e:
        log.warning("framing leaderboard fetch failed: %s", e)
        return {}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({str(k): v for k, v in out.items()}))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    framing = _fetch_leaderboard(args.season)
    log.info("loaded framing rows: %d", len(framing))

    conn = historical_db.connect()
    try:
        historical_db.apply_schema(conn)
        if args.force:
            where = "WHERE position = 'C'"
        else:
            where = "WHERE position = 'C' AND framing_runs IS NULL"
        cur = conn.execute(
            f"SELECT slate_date, mlb_id FROM player_slate {where}"
        )
        targets = cur.fetchall()
        log.info("catcher rows to populate: %d", len(targets))

        updates = 0
        misses = 0
        for t in targets:
            rec = framing.get(t["mlb_id"])
            if not rec:
                misses += 1
                continue
            historical_db.update_player_slate_columns(
                conn, t["slate_date"], t["mlb_id"], rec,
            )
            updates += 1
        conn.commit()
        log.info("UPDATE rows: %d (no leaderboard record: %d)", updates, misses)
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
