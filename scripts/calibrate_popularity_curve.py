"""Calibrate the V15 continuous popularity-score → EV multiplier curve.

Replaces the V14 discrete-bucket leverage system.  Walks historical
player rows (data/historical_players.csv), recomputes each row's
public-observable popularity score using the same input shape as
app/core/popularity.py, then reports per-score-level HV-rate vs
MP-rate to inform a continuous score → multiplier mapping.

This is a calibration script — it lives in /scripts/ and IS allowed to
read outcome columns (is_highest_value, is_most_popular, real_score)
because they're consumed only as analysis labels, never as inputs to a
runtime path.  The runtime side never reads outcome columns from this
CSV.

Usage:
    BO_CURRENT_SEASON=2026 python scripts/calibrate_popularity_curve.py
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.constants import (  # noqa: E402
    LEVERAGE_FAME_INDEX_DAYS,
    LEVERAGE_STAR_BATTER_OPS,
    LEVERAGE_STAR_PITCHER_ERA,
    PITCHER_POSITIONS,
    STAR_PLAYER_FLAGS,
    TEAM_MARKET_TIER,
    canonicalize_team,
)
from app.core.popularity import _normalize  # noqa: E402

CSV_PATH = ROOT / "data" / "historical_players.csv"


def _team_market_score(team: str) -> float:
    canonical = canonicalize_team(team)
    if canonical not in TEAM_MARKET_TIER:
        return 0.0  # only the live runtime raises; the calibration script tolerates drift
    tier = TEAM_MARKET_TIER[canonical]
    return {1: 3.0, 2: 2.0, 3: 1.0, 4: 0.0}[tier]


def _build_fame_index(rows: list[dict]) -> dict[tuple[str, str, date], int]:
    """Pre-compute (name, team, as_of) → MP appearances in trailing window.

    Mirrors app.core.popularity._load_fame_index but keyed across all
    dates for a single batched walk of the CSV.
    """
    by_player: dict[tuple[str, str], list[date]] = defaultdict(list)
    for row in rows:
        if row.get("is_most_popular") != "1":
            continue
        try:
            d = date.fromisoformat(row["date"])
        except (KeyError, ValueError):
            continue
        key = (_normalize(row["player_name"]), canonicalize_team(row["team"]))
        by_player[key].append(d)

    index: dict[tuple[str, str, date], int] = {}
    for key, dates in by_player.items():
        dates.sort()
        for d in dates:
            cutoff = d - timedelta(days=LEVERAGE_FAME_INDEX_DAYS)
            count = sum(1 for prior in dates if cutoff <= prior < d)
            index[(key[0], key[1], d)] = count
    return index


def _row_popularity_score(
    row: dict,
    fame_index: dict[tuple[str, str, date], int],
) -> float | None:
    """Compute the continuous popularity score for one historical row.

    Returns None if the row is unscoreable (e.g., missing date/team or a
    non-rookie batter with empty OPS — we can't estimate the field's
    fame for them retroactively without the elite-stat signal).

    Inputs (all public pre-game observables — same shape as the live
    predictor):
      * Team market tier
      * Star flag OR elite stats (ops_at_slate ≥ 0.900 batters; ERA not
        in the CSV — treated as None for pitchers)
      * Rolling 14-day MP fame index from prior-slate is_most_popular
      * Top-3 batting order — NOT in the CSV; deferred (the live path
        adds +1 for it; absent in calibration so the curve is slightly
        conservative for top-of-order batters, which is a forgivable
        miscalibration)
    """
    try:
        d = date.fromisoformat(row["date"])
    except (KeyError, ValueError):
        return None

    team = row.get("team", "")
    if not team:
        return None
    canonical = canonicalize_team(team)
    if canonical not in TEAM_MARKET_TIER:
        return None  # historical row with abbreviation drift — skip

    score = _team_market_score(team)

    name_norm = _normalize(row["player_name"])
    is_pitcher = (row.get("position") or "").upper() in PITCHER_POSITIONS

    if name_norm in STAR_PLAYER_FLAGS:
        score += 3.0
    else:
        ops_str = row.get("ops_at_slate", "")
        ops_at_slate: float | None = None
        if ops_str not in (None, ""):
            try:
                ops_at_slate = float(ops_str)
            except ValueError:
                ops_at_slate = None
        if not is_pitcher and ops_at_slate is not None and ops_at_slate >= LEVERAGE_STAR_BATTER_OPS:
            score += 2.0
        # Pitchers: ERA not in CSV; we conservatively skip the elite-stats branch.

    fame = fame_index.get((name_norm, canonical, d), 0)
    if fame >= 3:
        score += 2.0
    elif fame >= 1:
        score += 1.0

    # No batting_order column in the CSV — the +1 top-3 term is omitted.
    # Live runtime still applies it; calibration is slightly conservative
    # for top-of-order batters as a result.

    return score


def _bin_score(score: float) -> float:
    """Round to 0.5-step bin for aggregation."""
    return round(score * 2) / 2


def main() -> int:
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found", file=sys.stderr)
        return 1

    with CSV_PATH.open("r", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    print(f"Loaded {len(rows)} player-slate rows from {CSV_PATH.name}")

    fame_index = _build_fame_index(rows)
    print(f"Built fame index: {len(fame_index)} (player, team, date) entries")

    # Aggregate per score bin
    by_bin: dict[float, dict[str, float]] = defaultdict(lambda: {
        "n": 0, "n_hv": 0, "n_mp": 0, "rs_sum": 0.0, "rs_n": 0,
    })

    skipped = 0
    for row in rows:
        score = _row_popularity_score(row, fame_index)
        if score is None:
            skipped += 1
            continue
        bin_key = _bin_score(score)
        agg = by_bin[bin_key]
        agg["n"] += 1
        if row.get("is_highest_value") == "1":
            agg["n_hv"] += 1
        if row.get("is_most_popular") == "1":
            agg["n_mp"] += 1
        rs_str = row.get("real_score", "")
        if rs_str not in (None, ""):
            try:
                agg["rs_sum"] += float(rs_str)
                agg["rs_n"] += 1
            except ValueError:
                pass

    print(f"Skipped {skipped} unscoreable rows (date/team missing or unknown team)\n")

    # ---- Print per-bin table -------------------------------------------------
    print("=" * 92)
    print("Per-score-bin distribution (popularity-score → outcome rates):")
    print("=" * 92)
    print(f"{'score':>6}  {'n':>5}  {'%pool':>6}  {'hv_rate':>8}  {'mp_rate':>8}  "
          f"{'alpha':>7}  {'mean_rs':>8}")
    print("-" * 92)

    total_n = sum(agg["n"] for agg in by_bin.values())

    score_means: list[tuple[float, float]] = []  # (score, pool_weight)
    for score in sorted(by_bin):
        agg = by_bin[score]
        n = agg["n"]
        pct_pool = n / total_n * 100 if total_n else 0
        hv_rate = agg["n_hv"] / n if n else 0
        mp_rate = agg["n_mp"] / n if n else 0
        alpha = (hv_rate / mp_rate) if mp_rate > 0 else float("inf")
        mean_rs = agg["rs_sum"] / agg["rs_n"] if agg["rs_n"] else 0
        alpha_str = f"{alpha:>7.3f}" if alpha != float("inf") else f"{'∞':>7}"
        print(f"{score:>6.1f}  {n:>5}  {pct_pool:>5.1f}%  "
              f"{hv_rate:>7.1%}  {mp_rate:>7.1%}  {alpha_str}  {mean_rs:>8.3f}")
        score_means.append((score, n))

    # ---- Compute curve fit ---------------------------------------------------
    weighted_mean = sum(s * w for s, w in score_means) / total_n if total_n else 5.0

    # HV-rate by bin → fit a linear preference function:
    # the field drafts at mp_rate, but actual HV-rate doesn't track it 1:1.
    # We want multiplier(score) ∝ hv_rate(score) / mp_rate(score) ("alpha"),
    # normalised so that the pool-mean multiplier ≈ 1.0.
    print("\n" + "=" * 92)
    print("Recommended continuous-curve constants:")
    print("=" * 92)
    print(f"Weighted-mean popularity score across pool: {weighted_mean:.3f}")
    print(f"  → POPULARITY_NEUTRAL_SCORE = {weighted_mean:.2f}")

    # Fit a slope: pick a band [0.85, 1.20] (V14 inheritance) and scale slope
    # so the curve reaches FLOOR at the pool's 90th-pct score and CEILING at
    # the pool's 10th-pct score.  Empirical, monotone, no overfitting.
    sorted_rows = sorted(
        ((float(s), agg["n"]) for s, agg in by_bin.items()),
        key=lambda x: x[0],
    )
    cum = 0
    p10 = p90 = None
    for s, w in sorted_rows:
        cum += w
        if p10 is None and cum >= total_n * 0.10:
            p10 = s
        if p90 is None and cum >= total_n * 0.90:
            p90 = s
    print(f"Pool 10th pct: {p10}, 90th pct: {p90}")

    # multiplier(p90) = FLOOR; multiplier(p10) = CEILING
    # slope * (NEUTRAL - p90) = FLOOR - 1.0  → slope = (1.0 - FLOOR) / (p90 - NEUTRAL)
    floor = 0.85
    ceiling = 1.20
    if p90 is not None and p10 is not None and p90 > weighted_mean and p10 < weighted_mean:
        slope_floor = (1.0 - floor) / (p90 - weighted_mean)
        slope_ceiling = (ceiling - 1.0) / (weighted_mean - p10)
        # Use the average so both ends are roughly respected
        slope = (slope_floor + slope_ceiling) / 2
        print(f"  → POPULARITY_SLOPE = {slope:.4f}  "
              f"(floor-side {slope_floor:.4f}, ceiling-side {slope_ceiling:.4f})")
    else:
        print("  (cannot compute slope — pool 10th/90th pct unusable)")

    print(f"  → POPULARITY_MULT_FLOOR = {floor}")
    print(f"  → POPULARITY_MULT_CEILING = {ceiling}")

    # ---- Validate curve sanity ----------------------------------------------
    print("\n" + "=" * 92)
    print("Curve preview (multiplier at each score bin, given recommended constants):")
    print("=" * 92)
    if p90 is not None and p10 is not None and p90 > weighted_mean and p10 < weighted_mean:
        for s, _ in sorted_rows:
            mult = max(floor, min(ceiling, 1.0 + (weighted_mean - s) * slope))
            agg = by_bin[s]
            n = agg["n"]
            hv_rate = agg["n_hv"] / n if n else 0
            print(f"  score {s:>4.1f} (n={n:>4})  →  multiplier {mult:.3f}  |  hv_rate {hv_rate:>5.1%}")

    print("\nDone.  Paste constants into app/core/constants.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
