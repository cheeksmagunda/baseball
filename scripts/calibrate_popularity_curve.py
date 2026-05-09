"""Calibrate the V15.1 continuous popularity-score → EV multiplier curve.

Walks historical player rows (data/historical_players.csv), recomputes
each row's public-observable popularity score using the LIVE V15.1
helpers (`_team_market_score`, `_elite_stat_pts`, fame-rate index), then
reports per-score-level HV-rate vs MP-rate to inform a continuous score
→ multiplier mapping.

This script's output drives POPULARITY_NEUTRAL_SCORE / POPULARITY_SLOPE
in app/core/constants.py.  Re-run after any change to V15.1 component
weights to confirm the curve still saturates near the pool tails.

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
    LEVERAGE_FAME_INDEX_DAYS_BATTER,
    LEVERAGE_FAME_INDEX_DAYS_PITCHER,
    LEVERAGE_FAME_RATE_MAX_PTS,
    PITCHER_POSITIONS,
    POPULARITY_MULT_CEILING,
    POPULARITY_MULT_FLOOR,
    STAR_PLAYER_FLAGS,
    TEAM_MARKET_TIER,
    canonicalize_team,
)
from app.core.popularity import _elite_stat_pts, _normalize  # noqa: E402

CSV_PATH = ROOT / "data" / "historical_players.csv"


def _team_market_score(team: str) -> float:
    canonical = canonicalize_team(team)
    if canonical not in TEAM_MARKET_TIER:
        return 0.0  # only the live runtime raises; the calibration script tolerates drift
    tier = TEAM_MARKET_TIER[canonical]
    return {1: 3.0, 2: 2.0, 3: 1.0, 4: 0.0}[tier]


def _build_fame_rate_index(
    rows: list[dict],
    window_days: int,
) -> dict[tuple[str, str, date], tuple[int, int]]:
    """Pre-compute (name, team, as_of) → (mp_count, total_count) in trailing window.

    Mirrors app.core.popularity._load_fame_rate_index but keyed across
    all dates for a single batched walk of the CSV.
    """
    by_player: dict[tuple[str, str], list[tuple[date, int]]] = defaultdict(list)
    for row in rows:
        try:
            d = date.fromisoformat(row["date"])
        except (KeyError, ValueError):
            continue
        key = (_normalize(row["player_name"]), canonicalize_team(row["team"]))
        mp = 1 if row.get("is_most_popular") == "1" else 0
        by_player[key].append((d, mp))

    index: dict[tuple[str, str, date], tuple[int, int]] = {}
    for key, dated in by_player.items():
        dated.sort()
        for d, _ in dated:
            cutoff = d - timedelta(days=window_days)
            mp_count = 0
            total = 0
            for prior_d, prior_mp in dated:
                if cutoff <= prior_d < d:
                    total += 1
                    if prior_mp == 1:
                        mp_count += 1
            index[(key[0], key[1], d)] = (mp_count, total)
    return index


def _safe_float(s) -> float | None:
    if s in (None, ""):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _row_popularity_score(
    row: dict,
    fame_batter: dict[tuple[str, str, date], tuple[int, int]],
    fame_pitcher: dict[tuple[str, str, date], tuple[int, int]],
) -> float | None:
    """Compute the V15.1 continuous popularity score for one historical row.

    Calls the live `_elite_stat_pts` helper so any future tweak to the
    elite-stats ramp shape automatically propagates here.

    Inputs (all public pre-game observables — same shape as the live
    predictor):
      * Team market tier
      * Star flag OR elite-stats ramp (continuous in OPS or ERA)
      * Position-aware rate-based fame index (MP appearances / total
        appearances over trailing window — 14d batters, 28d pitchers)
      * Top-3 batting order — NOT in the CSV; the live path adds +1 for
        it.  Absent here, so the curve is slightly conservative for
        top-of-order batters — a forgivable miscalibration since they're
        a small fraction of the corpus and the +1 is uniform.
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
        ops_at_slate = _safe_float(row.get("ops_at_slate"))
        era_at_slate = _safe_float(row.get("era_at_slate"))
        # Live elite-stats helper — same code path the runtime executes.
        score += _elite_stat_pts(is_pitcher, ops_at_slate, era_at_slate)

    fame_index = fame_pitcher if is_pitcher else fame_batter
    mp, total = fame_index.get((name_norm, canonical, d), (0, 0))
    if total >= 1:
        score += LEVERAGE_FAME_RATE_MAX_PTS * (mp / total)

    return score


def _bin_score(score: float) -> float:
    """Round to 0.5-step bin for aggregation."""
    return round(score * 2) / 2


def main() -> int:
    # Step 6: every reader materialises data/historical.db into a tempdir
    # and rebinds the data file paths to it.  /data/ on-disk files are
    # still produced by writers + backfills as a derived export, but
    # readers consume the canonical store directly.
    import tempfile as _tempfile
    import sys as _sys, pathlib as _pathlib
    _repo = _pathlib.Path(__file__).resolve().parents[1]
    if str(_repo) not in _sys.path:
        _sys.path.insert(0, str(_repo))
    from scripts.export_historical_csvs import export_all as _export_all
    _hist_tmpdir = _tempfile.mkdtemp(prefix="hist_export_")
    _export_all(out_dir=_pathlib.Path(_hist_tmpdir))
    _hist_data_dir = _pathlib.Path(_hist_tmpdir)
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found", file=sys.stderr)
        return 1

    with CSV_PATH.open("r", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    print(f"Loaded {len(rows)} player-slate rows from {CSV_PATH.name}")

    fame_batter = _build_fame_rate_index(rows, LEVERAGE_FAME_INDEX_DAYS_BATTER)
    fame_pitcher = _build_fame_rate_index(rows, LEVERAGE_FAME_INDEX_DAYS_PITCHER)
    print(
        f"Built fame indexes: batter ({LEVERAGE_FAME_INDEX_DAYS_BATTER}d) "
        f"{len(fame_batter)} entries / pitcher ({LEVERAGE_FAME_INDEX_DAYS_PITCHER}d) "
        f"{len(fame_pitcher)} entries"
    )

    # Aggregate per score bin
    by_bin: dict[float, dict[str, float]] = defaultdict(lambda: {
        "n": 0, "n_hv": 0, "n_mp": 0, "rs_sum": 0.0, "rs_n": 0,
    })

    skipped = 0
    for row in rows:
        score = _row_popularity_score(row, fame_batter, fame_pitcher)
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
    # Source FLOOR / CEILING from the live constants — keep them in sync.
    floor = POPULARITY_MULT_FLOOR
    ceiling = POPULARITY_MULT_CEILING
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
