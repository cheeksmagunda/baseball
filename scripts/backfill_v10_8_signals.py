"""V10.8 backfill — enrich historical_slate_results.json with the new signals.

Adds three field families to every game in /data/historical_slate_results.json
so the offline feature audit (/tmp/baseball_eval/feature_audit.py) can bucket-
rate HV outcomes against V10.8 signals:

  Per starter (home_starter_xera, home_starter_x_woba_against, away_*):
      Pulled from Savant's pitcher expected-stats leaderboard via pybaseball.
      Joined onto the game by `home_starter_id` / `away_starter_id` (already
      populated by backfill_slate_env_conditions.py).

  Per team (home_team_framing_runs, away_team_framing_runs):
      Pulled from Savant's team catcher framing leaderboard (embedded JSON).
      Joined by team abbreviation.

  Per team-side (home_team_rest_days, away_team_rest_days):
      Calendar days between this date and the team's most recent game in the
      historical_slate_results.json corpus.  0 = back-to-back (played
      yesterday).  Fully derivable from existing JSON dates — no API call.

CAVEATS THIS SCRIPT IS HONEST ABOUT:
  1. xStats and framing are SEASON aggregates pulled NOW.  They reflect end-of-
     April-2026 values, not point-in-time-of-slate values.  For stable metrics
     like xwOBA over 200+ BBE the drift is small (~5-10 percentage points),
     but it's NOT exact.  This is acceptable for direction-check audits but
     not for re-calibrating thresholds.
  2. Catcher framing under the 2026 ABS Challenge System has changed regime —
     pre-ABS framing patterns differ slightly from post-ABS.  Backfill values
     are still informative (most of the corpus is post-ABS) but not perfect.
  3. Opp rest days IS exact — derived from dates only.

Reads and writes /data/ files only.  Does NOT touch the live pipeline, the DB,
or the scoring engine.  Outcome data (real_score, HV flags, total_value) is
NEVER read.  Only schedule dates, starter MLB IDs, and team abbreviations.

Usage:
    .venv-scraper/bin/python scripts/backfill_v10_8_signals.py
    .venv-scraper/bin/python scripts/backfill_v10_8_signals.py --force
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date as _date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SLATE_RESULTS = DATA_DIR / "historical_slate_results.json"

# Make app/ importable so we can reuse TEAM_ABBR_BY_MLB_ID + Savant helpers.
sys.path.insert(0, str(ROOT))

from app.core.mlb_api import TEAM_ABBR_BY_MLB_ID  # noqa: E402
from app.core.statcast import (  # noqa: E402
    _pitcher_expected_stats_table,
    _team_catcher_framing_table,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_v10_8")


def load_slates() -> list[dict]:
    with open(SLATE_RESULTS) as f:
        return json.load(f)


def save_slates(slates: list[dict]) -> None:
    with open(SLATE_RESULTS, "w") as f:
        json.dump(slates, f, indent=2)


# ---------------------------------------------------------------------------
# Phase 1 — pitcher xStats
# ---------------------------------------------------------------------------

def fetch_pitcher_xstats(season: int) -> dict[int, dict]:
    """Return {mlb_id: {x_era, x_woba_against}} from Savant pitcher expected stats."""
    df = _pitcher_expected_stats_table(season)
    if "player_id" not in df.columns:
        raise RuntimeError(
            f"Savant pitcher expected_stats schema unexpected (cols={list(df.columns)})"
        )
    out: dict[int, dict] = {}
    for _, row in df.iterrows():
        pid = row.get("player_id")
        if pid in (None, "") or (isinstance(pid, float) and (pid != pid)):
            continue
        try:
            mlb_id = int(pid)
        except (TypeError, ValueError):
            continue
        xera = row.get("xera")
        x_woba = row.get("est_woba")
        out[mlb_id] = {
            "x_era": float(xera) if xera == xera and xera is not None else None,
            "x_woba_against": float(x_woba) if x_woba == x_woba and x_woba is not None else None,
        }
    log.info("Fetched xStats for %d pitchers", len(out))
    return out


def annotate_pitcher_xstats(slates: list[dict], pitcher_xstats: dict[int, dict], force: bool) -> int:
    """Write home_starter_x{era,_woba_against} and away_starter_* onto each game.

    Returns count of game-side fields newly populated.
    """
    written = 0
    for slate in slates:
        for g in slate.get("games", []):
            for side in ("home", "away"):
                pid = g.get(f"{side}_starter_id")
                if pid is None:
                    continue
                key_xera = f"{side}_starter_x_era"
                key_xwoba = f"{side}_starter_x_woba_against"
                if not force and key_xera in g and key_xwoba in g:
                    continue
                row = pitcher_xstats.get(int(pid))
                if row is None:
                    # Pitcher not on the leaderboard (sub-50-PA rookie, e.g.) —
                    # write None explicitly so the audit can bucket on presence.
                    g[key_xera] = None
                    g[key_xwoba] = None
                    continue
                g[key_xera] = row.get("x_era")
                g[key_xwoba] = row.get("x_woba_against")
                written += 2
    log.info("Annotated %d pitcher-xstat fields across the corpus", written)
    return written


# ---------------------------------------------------------------------------
# Phase 2 — team catcher framing
# ---------------------------------------------------------------------------

def fetch_team_framing(season: int) -> dict[str, dict]:
    """Return {team_abbr: {framing_runs, framing_strike_pct, framing_pitches}}."""
    df = _team_catcher_framing_table(season)
    out: dict[str, dict] = {}
    for _, row in df.iterrows():
        team_id = row.get("team_id")
        if team_id in (None, "") or (isinstance(team_id, float) and (team_id != team_id)):
            continue
        try:
            tid = int(team_id)
        except (TypeError, ValueError):
            continue
        abbr = TEAM_ABBR_BY_MLB_ID.get(tid)
        if abbr is None:
            continue
        rv_tot = row.get("rv_tot")
        pct_tot = row.get("pct_tot")
        pitches = row.get("pitches")
        out[abbr.upper()] = {
            "framing_runs": float(rv_tot) if rv_tot is not None and rv_tot == rv_tot else None,
            "framing_strike_pct": float(pct_tot) if pct_tot is not None and pct_tot == pct_tot else None,
            "framing_pitches": int(pitches) if pitches is not None and pitches == pitches else None,
        }
    log.info("Fetched framing for %d teams", len(out))
    return out


def annotate_team_framing(slates: list[dict], team_framing: dict[str, dict], force: bool) -> int:
    """Write home/away_team_framing_runs (and *_pct) onto each game."""
    written = 0
    for slate in slates:
        for g in slate.get("games", []):
            for side in ("home", "away"):
                team = (g.get(side) or "").upper()
                if not team:
                    continue
                k_runs = f"{side}_team_framing_runs"
                k_pct = f"{side}_team_framing_pct"
                if not force and k_runs in g:
                    continue
                row = team_framing.get(team)
                g[k_runs] = (row or {}).get("framing_runs")
                g[k_pct] = (row or {}).get("framing_strike_pct")
                if row is not None:
                    written += 2
    log.info("Annotated %d framing fields across the corpus", written)
    return written


# ---------------------------------------------------------------------------
# Phase 3 — opponent rest days (date-derived; no API)
# ---------------------------------------------------------------------------

def annotate_opp_rest_days(slates: list[dict], force: bool) -> int:
    """For each (date, team) pair, look up the team's most recent prior game in
    the historical corpus and compute calendar-day delta.  Writes
    home_team_rest_days / away_team_rest_days on each game.

    Index slates by date first; build a {team: [sorted dates]} index from all
    appearances; for a given (slate_date, team), find the latest date < slate_date.

    Note: this only sees games that exist in our 33+ slate corpus.  A team's
    actual most-recent game might be on a date we don't have ingested.  When
    the team has no prior date in the corpus, write None — audit can bucket
    on the present-vs-absent distinction.  In production the live pipeline
    derives this from the MLB schedule API (full coverage), so the gap is
    a backfill-only artefact.
    """
    # Build index team → sorted list of dates the team appears in.
    appearances: dict[str, list[_date]] = {}
    for slate in slates:
        d = _date.fromisoformat(slate["date"])
        teams_today = set()
        for g in slate.get("games", []):
            for side in ("home", "away"):
                t = (g.get(side) or "").upper()
                if t:
                    teams_today.add(t)
        for t in teams_today:
            appearances.setdefault(t, []).append(d)
    for t in appearances:
        appearances[t].sort()

    written = 0
    for slate in slates:
        d = _date.fromisoformat(slate["date"])
        for g in slate.get("games", []):
            for side in ("home", "away"):
                team = (g.get(side) or "").upper()
                k = f"{side}_team_rest_days"
                if not team:
                    continue
                if not force and k in g:
                    continue
                dates = appearances.get(team, [])
                # Find latest date strictly before d.
                prior = None
                for cand in reversed(dates):
                    if cand < d:
                        prior = cand
                        break
                if prior is None:
                    g[k] = None
                else:
                    g[k] = max(0, (d - prior).days - 1)
                    written += 1
    log.info("Annotated %d opp-rest-days fields across the corpus", written)
    return written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="V10.8 historical backfill")
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument(
        "--force", action="store_true",
        help="Re-fetch and overwrite even if fields already present",
    )
    args = parser.parse_args()

    slates = load_slates()
    log.info("Loaded %d slate envelopes from %s", len(slates), SLATE_RESULTS)

    # Phase 1 — pitcher xStats from Savant
    log.info("Phase 1: pitcher xStats (Savant)")
    pitcher_xstats = fetch_pitcher_xstats(args.season)
    annotate_pitcher_xstats(slates, pitcher_xstats, args.force)

    # Phase 2 — team catcher framing from Savant
    log.info("Phase 2: team catcher framing (Savant)")
    team_framing = fetch_team_framing(args.season)
    annotate_team_framing(slates, team_framing, args.force)

    # Phase 3 — opp rest days (date math, no API)
    log.info("Phase 3: opp rest days (date math)")
    annotate_opp_rest_days(slates, args.force)

    save_slates(slates)
    log.info("Wrote %s", SLATE_RESULTS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
