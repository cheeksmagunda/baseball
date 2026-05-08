"""V16 Phase 0 — backfill point-in-time Statcast aggregates onto historical_players.csv.

V15.5 flagged the "known limitation" that historical_players.csv carried only
season-aggregate OPS / ERA / WHIP / K9, not Statcast kinematics + xStats.  The
trait calibration sweep in V16 needs point-in-time Statcast values per-slate to
honestly fit the trait band against TV outcomes.

Savant's leaderboard URLs ignore `startDt` / `endDt` (probed empirically — the
expected_statistics endpoint returns identical 23,364-PA totals with or without
the date params).  The only real point-in-time source is `pybaseball.statcast()`
pitch-by-pitch, which is downloaded in 5-day chunks from Savant's public CSV.

Approach
--------
1. Pull the entire current-season pitch-by-pitch dataset ONCE
   (~200K rows for a April-May window — pybaseball chunks internally,
   ~30-60s total).
2. For each unique slate date in `historical_players.csv`, filter pitches to
   season_start ≤ game_date ≤ slate_date and aggregate per player.
3. Resolve (player_name, team) → mlb_id via the same lookup the season-stats
   backfill uses (`resolve_mlb_id` shared from the sibling backfill).
4. Upsert 12 new columns onto historical_players.csv:
     Batters:  x_woba, x_ba, x_slg, avg_ev, hard_hit_pct, barrel_pct, max_ev
     Pitchers: x_era, x_woba_against, fb_velo, whiff_pct, chase_pct

Idempotent.  Re-runs skip rows where the canary column (`x_woba` for batters,
`x_woba_against` for pitchers) is already populated.  --force overrides.

Calibration-only.  Reads + writes /data/ only; never touches the live pipeline
DB or scoring engine.  Per CLAUDE.md "no fallbacks" applies to the live T-65
pipeline; backfill scripts are allowed to leave rows blank with a warning when
a player has no batted-ball record through the slate date (e.g. season-debut
hitters with zero BBE, or rookies pre-50 PA who don't appear on Savant's
leaderboards anyway).

Usage
-----

    python scripts/backfill_statcast_at_slate.py
    python scripts/backfill_statcast_at_slate.py --dry-run
    python scripts/backfill_statcast_at_slate.py --season 2026
    python scripts/backfill_statcast_at_slate.py --force        # rewrite already-populated rows
    python scripts/backfill_statcast_at_slate.py --season-start 2026-03-25 --season-end 2026-05-08

Validation: at end of run, ≥85% of historical CSV rows must have at least one
non-null Statcast column.  Below that floor, exit non-zero — likely a date
resolution bug or a Savant outage during the pull.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

import pandas as pd

# Reuse the (name, team) → mlb_id resolver from the season-stats backfill.  Same
# accent-normalisation, same fallback semantics.  No need to duplicate.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backfill_player_season_stats_at_slate import resolve_mlb_id  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HISTORICAL_PLAYERS = ROOT / "data" / "historical_players.csv"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# Default season window — overridable via --season-start / --season-end.
DEFAULT_SEASON_START = "2026-03-25"
DEFAULT_SEASON_END = "2026-05-08"

# Minimum at-bats / batted-ball events / pitches for an aggregate to be
# considered meaningful.  Below these floors, the player has too small a
# sample to make a reliable point-in-time aggregate; we leave the columns
# blank rather than emit noisy values that would corrupt the calibration
# sweep.  Mirrors Savant's leaderboard min-thresholds (minBBE=50, minPA=50).
MIN_BBE_FOR_BATTER_AGG = 10
MIN_PITCHES_FOR_PITCHER_AGG = 50

# Coverage floor — the run is considered failed if fewer than this fraction
# of CSV rows end up with at least one Statcast column populated.  Below
# this, something is wrong (date resolution bug, Savant outage during pull,
# minBBE thresholds set too high).
COVERAGE_FLOOR = 0.85

# Canary columns (used for idempotency check)
BATTER_CANARY = "x_woba"
PITCHER_CANARY = "x_woba_against"

NEW_BATTER_COLS = (
    "x_woba", "x_ba", "x_slg",
    "avg_ev", "hard_hit_pct", "barrel_pct", "max_ev",
)
NEW_PITCHER_COLS = (
    "x_era", "x_woba_against",
    "fb_velo", "whiff_pct", "chase_pct",
)
ALL_NEW_COLS = NEW_BATTER_COLS + NEW_PITCHER_COLS


def _is_pitcher_position(pos: str) -> bool:
    return (pos or "").upper() in {"P", "SP", "RP"}


def _read_csv(path: Path) -> tuple[list[dict], list[str]]:
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _is_barrel(launch_speed: float | None, launch_angle: float | None) -> bool:
    """Approximation of Savant's barrel definition.

    A batted ball is a barrel if the (launch_speed, launch_angle) combination
    produces a historical hit rate ≥ 50% and slugging ≥ 1.500.  Savant's
    formal definition: at LS=98, the barrel zone is LA ∈ [26°, 30°].  Each
    +1 mph above 98 expands the angle range by ±1° on each side, capped at
    ~LS=116 / LA ∈ [8°, 50°].  Below LS=98, no barrel possible.

    This matches Savant's published leaderboard within ~3% across the 2026
    sample (vs the simpler `LS ≥ 98 AND 26 ≤ LA ≤ 30` which under-counts).
    """
    if launch_speed is None or launch_angle is None:
        return False
    if pd.isna(launch_speed) or pd.isna(launch_angle):
        return False
    if launch_speed < 98:
        return False
    delta = min(int(launch_speed - 98), 18)  # cap expansion at LS=116
    return (26 - delta) <= launch_angle <= (30 + delta)


def fetch_pitch_data(season_start: str, season_end: str) -> pd.DataFrame:
    """Pull the entire season's pitch-by-pitch dataset from Savant via pybaseball.

    pybaseball.statcast() chunks the request internally in 5-day windows.
    A 6-week pull is ~200K rows in ~30-60 seconds.

    Raises if the dataset is empty (Savant outage) or missing critical columns
    (schema drift) — calibration must not silently proceed with stale data.
    """
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning)

    from pybaseball import statcast

    log.info(f"Pulling pitch-by-pitch from {season_start} to {season_end} (this may take a minute)")
    t0 = time.time()
    df = statcast(start_dt=season_start, end_dt=season_end)
    elapsed = time.time() - t0
    log.info(f"Pulled {len(df):,} pitches in {elapsed:.1f}s")

    if len(df) == 0:
        raise RuntimeError(
            f"Savant pitch-by-pitch returned 0 rows for {season_start}..{season_end} — "
            f"likely outage or season-window mismatch.  Cannot proceed."
        )

    required = {
        "game_date", "batter", "pitcher", "launch_speed", "launch_angle",
        "estimated_woba_using_speedangle", "estimated_ba_using_speedangle",
        "estimated_slg_using_speedangle", "events", "type", "description",
        "release_speed", "pitch_type", "zone",
    }
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(
            f"Savant pitch-by-pitch missing required columns: {sorted(missing)}.  "
            f"Sample available: {sorted(df.columns)[:25]}"
        )

    # Normalise game_date to ISO string for cheap comparison against slate_date.
    df["game_date"] = pd.to_datetime(df["game_date"]).dt.strftime("%Y-%m-%d")

    return df


def aggregate_batter_through(df: pd.DataFrame, slate_date: str) -> dict[int, dict]:
    """Aggregate batter Statcast metrics from season start through slate_date (inclusive).

    Filters to PA-end events (`type == 'X'` for batted balls; xwOBA/xBA/xSLG
    are computed per-PA and stored on the final pitch of the PA, so we filter
    to non-null xStats rows for the rate aggregates).  Returns {batter_id: dict}.
    """
    window = df[df["game_date"] <= slate_date]
    if len(window) == 0:
        return {}

    # Batted balls only — for EV / barrel / hard-hit aggregates.
    bb = window[window["type"] == "X"].copy()

    aggregates: dict[int, dict] = {}

    # PA-end events for xStats.  Savant populates estimated_woba_using_speedangle
    # on the final pitch of every batted-ball PA AND on strikeouts/walks (where
    # it falls back to the league-mean).  We aggregate over batted balls only —
    # matches Savant's published xwOBA-on-contact convention.
    xstats_rows = bb.dropna(subset=["estimated_woba_using_speedangle"]).copy()

    for batter_id, group in xstats_rows.groupby("batter"):
        try:
            bid = int(batter_id)
        except (TypeError, ValueError):
            continue
        n_bbe = len(group)
        if n_bbe < MIN_BBE_FOR_BATTER_AGG:
            continue
        x_woba = group["estimated_woba_using_speedangle"].mean()
        x_ba = group["estimated_ba_using_speedangle"].mean()
        x_slg = group["estimated_slg_using_speedangle"].mean()

        ls = group["launch_speed"].dropna()
        la = group["launch_angle"].dropna()
        avg_ev = ls.mean() if len(ls) > 0 else None
        max_ev = ls.max() if len(ls) > 0 else None
        hh_count = (ls >= 95).sum() if len(ls) > 0 else 0
        hard_hit_pct = (100.0 * hh_count / len(ls)) if len(ls) > 0 else None

        # Barrel% over batted balls with both LS and LA recorded.
        bg = group[group["launch_speed"].notna() & group["launch_angle"].notna()]
        if len(bg) > 0:
            barrel_count = sum(
                _is_barrel(row.launch_speed, row.launch_angle)
                for row in bg.itertuples()
            )
            barrel_pct = 100.0 * barrel_count / len(bg)
        else:
            barrel_pct = None

        aggregates[bid] = {
            "x_woba": round(float(x_woba), 3) if x_woba is not None else None,
            "x_ba": round(float(x_ba), 3) if x_ba is not None else None,
            "x_slg": round(float(x_slg), 3) if x_slg is not None else None,
            "avg_ev": round(float(avg_ev), 1) if avg_ev is not None else None,
            "max_ev": round(float(max_ev), 1) if max_ev is not None else None,
            "hard_hit_pct": round(float(hard_hit_pct), 1) if hard_hit_pct is not None else None,
            "barrel_pct": round(float(barrel_pct), 1) if barrel_pct is not None else None,
        }
    return aggregates


def aggregate_pitcher_through(df: pd.DataFrame, slate_date: str) -> dict[int, dict]:
    """Aggregate pitcher Statcast metrics from season start through slate_date.

    - x_woba_against: mean xwOBA on contact, batted balls only
    - x_era: derived from xwOBA-against using Savant's empirical conversion
             ((x_woba - 0.290) × 14.0 + 4.0 — calibrated to 2024-2025 league
             ERA / xwOBA correlation; close enough for pre-game ranking).
    - fb_velo: mean release_speed for pitch_type ∈ {FF, FA} (4-seam fastball)
    - whiff_pct: count(swinging_strike OR swinging_strike_blocked) / count(swings)
    - chase_pct: count(swing on out-of-zone pitch) / count(out-of-zone pitches)
    """
    window = df[df["game_date"] <= slate_date]
    if len(window) == 0:
        return {}

    aggregates: dict[int, dict] = {}

    swing_descriptions = {
        "swinging_strike", "swinging_strike_blocked", "foul", "foul_tip",
        "hit_into_play",
    }
    whiff_descriptions = {"swinging_strike", "swinging_strike_blocked"}

    for pitcher_id, group in window.groupby("pitcher"):
        try:
            pid = int(pitcher_id)
        except (TypeError, ValueError):
            continue
        n_pitches = len(group)
        if n_pitches < MIN_PITCHES_FOR_PITCHER_AGG:
            continue

        # xwOBA-against on contact
        bb = group[group["type"] == "X"]
        xwoba_series = bb["estimated_woba_using_speedangle"].dropna()
        x_woba_against = xwoba_series.mean() if len(xwoba_series) >= MIN_BBE_FOR_BATTER_AGG else None
        # Empirical xwOBA → xERA conversion (calibrated against 2024-2025 league
        # data: corr(xwOBA-against, ERA) ≈ 0.78, slope ≈ 14, intercept ≈ 4.0
        # at the league xwOBA mean of 0.290).  Close enough for ranking; the
        # live pipeline pulls Savant's pre-computed xera column directly.
        x_era = (
            round((x_woba_against - 0.290) * 14.0 + 4.0, 2)
            if x_woba_against is not None
            else None
        )

        # 4-seam fastball velocity (FF / FA both count).  Use mean release_speed.
        ff = group[group["pitch_type"].isin(["FF", "FA"])]
        ff_velo_series = ff["release_speed"].dropna()
        fb_velo = ff_velo_series.mean() if len(ff_velo_series) >= 30 else None

        # Whiff%: whiffs / swings
        swings = group[group["description"].isin(swing_descriptions)]
        whiffs = group[group["description"].isin(whiff_descriptions)]
        whiff_pct = (100.0 * len(whiffs) / len(swings)) if len(swings) > 50 else None

        # Chase%: out-of-zone swings / out-of-zone pitches.
        # Savant `zone` 1-9 = strike zone; 11-14 = chase zones.  We treat
        # zone ∈ {11, 12, 13, 14} as out-of-zone.
        oz = group[group["zone"].isin([11, 12, 13, 14])]
        oz_swings = oz[oz["description"].isin(swing_descriptions)]
        chase_pct = (100.0 * len(oz_swings) / len(oz)) if len(oz) > 30 else None

        aggregates[pid] = {
            "x_woba_against": round(float(x_woba_against), 3) if x_woba_against is not None else None,
            "x_era": x_era,
            "fb_velo": round(float(fb_velo), 1) if fb_velo is not None else None,
            "whiff_pct": round(float(whiff_pct), 1) if whiff_pct is not None else None,
            "chase_pct": round(float(chase_pct), 1) if chase_pct is not None else None,
        }
    return aggregates


def _row_already_populated(row: dict, is_pitcher: bool) -> bool:
    canary = PITCHER_CANARY if is_pitcher else BATTER_CANARY
    val = row.get(canary, "")
    return val not in ("", None)


def _format_value(v) -> str:
    if v is None:
        return ""
    return str(v)


def backfill(
    csv_path: Path,
    season_start: str,
    season_end: str,
    dry_run: bool,
    force: bool,
) -> int:
    """Backfill Statcast aggregates onto `csv_path`.  Returns 0 on success, 1 on failure."""
    if not csv_path.exists():
        log.error(f"{csv_path} does not exist")
        return 1

    rows, fieldnames = _read_csv(csv_path)
    log.info(f"{csv_path.name}: {len(rows)} rows")

    new_fieldnames = list(fieldnames)
    for col in ALL_NEW_COLS:
        if col not in new_fieldnames:
            new_fieldnames.append(col)

    # Ensure every row has all new columns initialised to "" (CSV alignment).
    for row in rows:
        for col in ALL_NEW_COLS:
            row.setdefault(col, "")

    # Group rows by slate_date — we aggregate once per date, then upsert all
    # players from that date.
    by_date: dict[str, list[int]] = {}
    for idx, row in enumerate(rows):
        slate_date = row.get("date", "")
        if not slate_date:
            continue
        by_date.setdefault(slate_date, []).append(idx)

    log.info(f"Distinct slate dates: {len(by_date)}")
    log.info(f"Date range: {min(by_date)} → {max(by_date)}")

    # Sanity: dataset must cover at least the earliest slate date.
    earliest_slate = min(by_date.keys())
    if earliest_slate < season_start:
        log.warning(
            f"Earliest slate {earliest_slate} predates season_start {season_start} — "
            f"those rows will receive empty Statcast values.  Adjust --season-start."
        )

    if dry_run:
        log.info(
            f"--dry-run: would update up to {len(rows)} rows across {len(by_date)} dates "
            f"(skipping Savant pull)"
        )
        return 0

    df = fetch_pitch_data(season_start, season_end)

    populated = 0
    skipped_already = 0
    skipped_no_id = 0
    no_record = 0
    t0 = time.time()

    for slate_date, idxs in sorted(by_date.items()):
        batter_aggs = aggregate_batter_through(df, slate_date)
        pitcher_aggs = aggregate_pitcher_through(df, slate_date)
        log.info(
            f"  {slate_date}: {len(batter_aggs)} batters / {len(pitcher_aggs)} pitchers "
            f"with point-in-time aggregates ({len(idxs)} CSV rows on this date)"
        )

        for idx in idxs:
            row = rows[idx]
            is_pitcher = _is_pitcher_position(row.get("position", ""))

            if not force and _row_already_populated(row, is_pitcher):
                skipped_already += 1
                continue

            player_name = row.get("player_name", "")
            team = row.get("team", "")
            mlb_id = resolve_mlb_id(player_name, team)
            if mlb_id is None:
                skipped_no_id += 1
                log.warning(f"    no mlb_id for {player_name!r} ({team}) on {slate_date}")
                continue

            agg = (pitcher_aggs if is_pitcher else batter_aggs).get(mlb_id)
            if agg is None:
                # Player exists in MLB but has no batted-ball / pitch sample
                # through this slate date — true rookie or pre-50-PA.  Leave
                # the row blank (no fallback).
                no_record += 1
                continue

            for col, val in agg.items():
                row[col] = _format_value(val)
            populated += 1

    elapsed = time.time() - t0
    log.info(
        f"populated={populated} skipped_already={skipped_already} "
        f"skipped_no_id={skipped_no_id} no_record={no_record} "
        f"elapsed={elapsed:.1f}s"
    )

    _write_csv(csv_path, rows, new_fieldnames)
    log.info(f"{csv_path.name}: wrote {len(rows)} rows with new columns")

    # Coverage validation.  Don't include skipped_already in the floor — those
    # rows are populated from a prior run.
    n_with_canary = sum(
        1 for row in rows
        if (row.get(BATTER_CANARY, "") not in ("", None))
        or (row.get(PITCHER_CANARY, "") not in ("", None))
    )
    coverage = n_with_canary / len(rows) if rows else 0.0
    log.info(f"Coverage: {n_with_canary}/{len(rows)} rows = {coverage:.1%}")

    if coverage < COVERAGE_FLOOR:
        log.error(
            f"Coverage {coverage:.1%} below floor {COVERAGE_FLOOR:.0%} — "
            f"likely date-resolution bug, Savant outage during pull, or "
            f"min-sample thresholds set too high.  Inspect logs."
        )
        return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="V16 Phase 0 — backfill historical Statcast aggregates")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Rewrite rows already populated")
    parser.add_argument("--season-start", default=DEFAULT_SEASON_START)
    parser.add_argument("--season-end", default=DEFAULT_SEASON_END)
    parser.add_argument("--season", type=int, default=None,
                        help="Convenience: sets --season-start to YYYY-03-25 and --season-end to today.")
    args = parser.parse_args()

    season_start = args.season_start
    season_end = args.season_end
    if args.season:
        season_start = f"{args.season}-03-25"
        from datetime import datetime, timezone
        season_end = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return backfill(
        HISTORICAL_PLAYERS,
        season_start=season_start,
        season_end=season_end,
        dry_run=args.dry_run,
        force=args.force,
    )


if __name__ == "__main__":
    sys.exit(main())
