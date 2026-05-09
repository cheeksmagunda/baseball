"""Calibrate the V15.1 continuous popularity-score COMPONENTS.

Companion to scripts/calibrate_popularity_curve.py.  That script
calibrates the score → multiplier mapping (V15 ship).  This script
calibrates the inputs that go INTO the score — replacing the V14-era
binary thresholds (ERA <= 3.00, OPS >= 0.900, fame_count >= 1, fame_count
>= 3) with continuous functions fit against actual is_most_popular
outcomes from data/historical_players.csv.

Why: the V15 score-to-multiplier curve is calibrated, but the score
itself is built from binary thresholds inherited from V14 and never
re-fitted.  A pitcher at 3.51 ERA scores 0; a pitcher at 3.49 ERA scores
+2.  A pitcher MP'd in 1 of 2 starts scores +1; one MP'd in 2 of 2 also
scores +1.  These boundary cases produce the failure modes the user is
seeing (Bello vs Elder did not differentiate enough on 2026-05-05).

Inputs available in historical_players.csv (after May 2026 backfills):
    * date, player_name, team, position
    * ops_at_slate, iso_at_slate (batters; ~1237 rows populated)
    * era_at_slate, whip_at_slate, k9_at_slate (pitchers; ~268 rows populated)
    * is_most_popular (the outcome label we're fitting against)

Inputs NOT in the CSV (deferred — runtime path keeps the existing
heuristic for these two):
    * Top-3 batting order (would require backfilling historical lineup
      cards, ~28 slates × 14-15 games × 9 spots = ~3500 lookups).
    * STAR_PLAYER_FLAGS membership is a curated list — its contribution
      is reported but not "fit" (you can't fit a binary curated label).

Calibration-only.  This script reads outcome columns
(is_most_popular, is_highest_value, real_score) for analysis purposes
only.  The runtime path (app/core/popularity.py) reads only
is_most_popular from prior dates strictly before the current slate, and
that exempt read is bounded by the audit script.

Usage:
    python scripts/calibrate_popularity_components.py
    python scripts/calibrate_popularity_components.py --validate-current
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.constants import (  # noqa: E402
    LEVERAGE_FAME_INDEX_DAYS,
    LEVERAGE_FAME_INDEX_DAYS_PITCHER,
    LEVERAGE_FAME_RATE_MAX_PTS,
    LEVERAGE_STAR_BATTER_OPS,
    LEVERAGE_STAR_PITCHER_ERA,
    PITCHER_POSITIONS,
    STAR_PLAYER_FLAGS,
    TEAM_MARKET_TIER,
    canonicalize_team,
)
from app.core.popularity import _elite_stat_pts, _normalize  # noqa: E402

CSV_PATH = ROOT / "data" / "historical_players.csv"

# Pitchers don't pitch every day, so a 14-day window (~2-3 starts) is
# too sparse for the rate-based fame index to be stable.  A 28-day
# window (~5-6 starts) gives a meaningful denominator.  Imported here
# for readability; sources the same value as constants.py.
PITCHER_FAME_INDEX_DAYS = LEVERAGE_FAME_INDEX_DAYS_PITCHER


@dataclass
class Row:
    date: date
    name_norm: str
    team_canonical: str
    is_pitcher: bool
    is_most_popular: int
    is_highest_value: int
    real_score: float | None
    ops_at_slate: float | None
    era_at_slate: float | None


def _safe_float(s: str | None) -> float | None:
    if s in (None, ""):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _safe_int(s: str | None) -> int:
    if s in (None, ""):
        return 0
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def load_rows(path: Path) -> list[Row]:
    out: list[Row] = []
    with path.open("r", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                d = date.fromisoformat(r["date"])
            except (KeyError, ValueError):
                continue
            team_canon = canonicalize_team(r.get("team", ""))
            if not team_canon:
                continue
            pos = (r.get("position") or "").upper()
            out.append(Row(
                date=d,
                name_norm=_normalize(r.get("player_name", "")),
                team_canonical=team_canon,
                is_pitcher=pos in PITCHER_POSITIONS,
                is_most_popular=_safe_int(r.get("is_most_popular")),
                is_highest_value=_safe_int(r.get("is_highest_value")),
                real_score=_safe_float(r.get("real_score")),
                ops_at_slate=_safe_float(r.get("ops_at_slate")),
                era_at_slate=_safe_float(r.get("era_at_slate")),
            ))
    return out


# ---- Fame index (rate-based, position-aware window) ---------------------------


def build_fame_index(
    rows: list[Row],
    window_days: int,
) -> dict[tuple[str, str, date], tuple[int, int]]:
    """Return (player, team, as_of) → (mp_appearances, total_appearances) in trailing window.

    Both numerator and denominator are computed on the SAME corpus, which
    self-selects to "drafted players" (the CSV only contains MP / HV / 3X
    rows).  This means the rate is a "given you're a candidate, how often
    did the field draft you popular" — the right denominator for the
    calibration since we're fitting MP-flag risk on the same population.
    """
    by_player: dict[tuple[str, str], list[tuple[date, int]]] = defaultdict(list)
    for r in rows:
        by_player[(r.name_norm, r.team_canonical)].append((r.date, r.is_most_popular))

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


def fame_rate(
    fame_index: dict[tuple[str, str, date], tuple[int, int]],
    name_norm: str,
    team: str,
    d: date,
) -> tuple[float, int]:
    """Return (mp_rate, denominator) for this player at this date."""
    mp, total = fame_index.get((name_norm, team, d), (0, 0))
    if total == 0:
        return (0.0, 0)
    return (mp / total, total)


# ---- Bucket aggregation -------------------------------------------------------


def bucket_mp_rate(
    rows: list[Row],
    feature_fn: Callable[[Row], float | None],
    edges: list[float],
    label_fn: Callable[[float, float], str] | None = None,
) -> list[tuple[str, int, int, float]]:
    """Bucket rows by feature value, return (label, n, n_mp, mp_rate) per bucket."""
    buckets: list[list[Row]] = [[] for _ in range(len(edges) + 1)]
    for r in rows:
        v = feature_fn(r)
        if v is None:
            continue
        placed = False
        for i, edge in enumerate(edges):
            if v < edge:
                buckets[i].append(r)
                placed = True
                break
        if not placed:
            buckets[-1].append(r)

    out: list[tuple[str, int, int, float]] = []
    for i, bucket in enumerate(buckets):
        if i == 0:
            label = f"<{edges[0]}"
        elif i == len(edges):
            label = f">={edges[-1]}"
        else:
            label = f"[{edges[i-1]},{edges[i]})"
        if label_fn is not None and i > 0 and i < len(edges):
            label = label_fn(edges[i-1], edges[i])
        n = len(bucket)
        n_mp = sum(r.is_most_popular for r in bucket)
        mp_rate = n_mp / n if n else 0.0
        out.append((label, n, n_mp, mp_rate))
    return out


def print_bucket_table(title: str, rows_by_bucket: list[tuple[str, int, int, float]]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    print(f"{'bucket':>14}  {'n':>5}  {'n_mp':>5}  {'mp_rate':>8}")
    for label, n, n_mp, rate in rows_by_bucket:
        print(f"{label:>14}  {n:>5}  {n_mp:>5}  {rate:>7.1%}")


# ---- Per-component fits -------------------------------------------------------


def fit_market_tier(rows: list[Row]) -> None:
    print("\n" + "=" * 78)
    print("COMPONENT 1: Team market tier")
    print("=" * 78)
    by_tier: dict[int, list[Row]] = defaultdict(list)
    for r in rows:
        tier = TEAM_MARKET_TIER.get(r.team_canonical)
        if tier is None:
            continue
        by_tier[tier].append(r)
    print(f"{'tier':>4}  {'n':>5}  {'n_mp':>5}  {'mp_rate':>8}  recommended_score_pts")
    print("-" * 64)
    pool_n = sum(len(rs) for rs in by_tier.values())
    pool_mp_rate = sum(r.is_most_popular for rs in by_tier.values() for r in rs) / pool_n if pool_n else 0
    for tier in sorted(by_tier):
        rs = by_tier[tier]
        n = len(rs)
        n_mp = sum(r.is_most_popular for r in rs)
        rate = n_mp / n if n else 0
        # log-odds vs pool baseline
        delta = rate - pool_mp_rate
        # Recommended pts: scale 0..3 such that tier 1 (max signal) gets full credit
        # Keep the existing 3,2,1,0 mapping if data agrees, otherwise report.
        print(f"{tier:>4}  {n:>5}  {n_mp:>5}  {rate:>7.1%}  delta_vs_pool={delta:+.1%}")
    print(f"\nPool baseline MP-rate: {pool_mp_rate:.1%}")
    print("Recommended: KEEP existing tier→pts mapping {1:3, 2:2, 3:1, 4:0} if monotone.")


def fit_fame_rate(rows: list[Row], window_days: int, label: str) -> dict:
    print("\n" + "=" * 78)
    print(f"COMPONENT 2: Fame index (rate-based, {window_days}-day window) — {label}")
    print("=" * 78)
    fame_index = build_fame_index(rows, window_days)
    enriched: list[tuple[Row, float, int]] = []
    for r in rows:
        rate, denom = fame_rate(fame_index, r.name_norm, r.team_canonical, r.date)
        enriched.append((r, rate, denom))

    # Bucket by denom first to filter out players with no prior appearances
    no_prior = [(r, rate) for r, rate, denom in enriched if denom == 0]
    has_prior = [(r, rate) for r, rate, denom in enriched if denom >= 1]

    n_no = len(no_prior)
    n_no_mp = sum(r.is_most_popular for r, _ in no_prior)
    print(f"No prior appearances in window: n={n_no}  mp_rate={n_no_mp/n_no:.1%}" if n_no else "No prior appearances in window: n=0")

    n_has = len(has_prior)
    n_has_mp = sum(r.is_most_popular for r, _ in has_prior)
    print(f"Has prior appearances:          n={n_has}  mp_rate={n_has_mp/n_has:.1%}" if n_has else "")

    # For "has prior", bucket by mp_rate
    edges = [0.001, 0.25, 0.50, 0.75, 1.00]
    print(f"\nFor players with >=1 prior appearance, bucket by MP-rate over {window_days}d:")
    print(f"{'rate':>14}  {'n':>5}  {'n_mp':>5}  {'mp_rate':>8}")
    buckets: list[list[tuple[Row, float]]] = [[] for _ in range(len(edges))]
    for r, rate in has_prior:
        for i, e in enumerate(edges):
            if rate <= e:
                buckets[i].append((r, rate))
                break
    bucket_labels = ["0", "(0,0.25]", "(0.25,0.50]", "(0.50,0.75]", "(0.75,1.0]"]
    for label_, bucket in zip(bucket_labels, buckets):
        n = len(bucket)
        n_mp = sum(r.is_most_popular for r, _ in bucket)
        rate_ = n_mp / n if n else 0
        print(f"{label_:>14}  {n:>5}  {n_mp:>5}  {rate_:>7.1%}")

    return {"fame_index": fame_index}


def fit_era_at_slate(rows: list[Row]) -> None:
    pitchers = [r for r in rows if r.is_pitcher and r.era_at_slate is not None]
    print("\n" + "=" * 78)
    print(f"COMPONENT 3: Pitcher ERA at slate (continuous) — n={len(pitchers)}")
    print("=" * 78)
    edges = [2.50, 3.00, 3.50, 4.00, 4.50, 5.00]
    table = bucket_mp_rate(pitchers, lambda r: r.era_at_slate, edges)
    print(f"{'bucket':>14}  {'n':>5}  {'n_mp':>5}  {'mp_rate':>8}")
    for label, n, n_mp, rate in table:
        print(f"{label:>14}  {n:>5}  {n_mp:>5}  {rate:>7.1%}")

    # Also report using just the players with ERA > 0.5 to filter out
    # opening-week 0-IP small-sample noise.
    valid = [r for r in pitchers if r.era_at_slate is not None and r.era_at_slate > 0.0]
    print(f"\nFiltered to ERA > 0 (drops opening-week 0-IP rows): n={len(valid)}")
    table2 = bucket_mp_rate(valid, lambda r: r.era_at_slate, edges)
    print(f"{'bucket':>14}  {'n':>5}  {'n_mp':>5}  {'mp_rate':>8}")
    for label, n, n_mp, rate in table2:
        print(f"{label:>14}  {n:>5}  {n_mp:>5}  {rate:>7.1%}")


def fit_ops_at_slate(rows: list[Row]) -> None:
    batters = [r for r in rows if not r.is_pitcher and r.ops_at_slate is not None]
    print("\n" + "=" * 78)
    print(f"COMPONENT 4: Batter OPS at slate (continuous) — n={len(batters)}")
    print("=" * 78)
    edges = [0.500, 0.650, 0.750, 0.850, 0.950, 1.050]
    table = bucket_mp_rate(batters, lambda r: r.ops_at_slate, edges)
    print(f"{'bucket':>14}  {'n':>5}  {'n_mp':>5}  {'mp_rate':>8}")
    for label, n, n_mp, rate in table:
        print(f"{label:>14}  {n:>5}  {n_mp:>5}  {rate:>7.1%}")


def fit_star_flag(rows: list[Row]) -> None:
    print("\n" + "=" * 78)
    print("COMPONENT 5: STAR_PLAYER_FLAGS membership")
    print("=" * 78)
    flagged = [r for r in rows if r.name_norm in STAR_PLAYER_FLAGS]
    not_flagged = [r for r in rows if r.name_norm not in STAR_PLAYER_FLAGS]
    for label, group in [("Flagged star", flagged), ("Not flagged", not_flagged)]:
        n = len(group)
        n_mp = sum(r.is_most_popular for r in group)
        rate = n_mp / n if n else 0
        print(f"  {label:<14}  n={n:>5}  mp_rate={rate:.1%}")


# ---- Continuous score fit + AUC ------------------------------------------------


def auc_for_score(scores_and_labels: list[tuple[float, int]]) -> float:
    """Compute AUC for a binary classifier via ranks.  Mann-Whitney U."""
    pos = [s for s, label in scores_and_labels if label == 1]
    neg = [s for s, label in scores_and_labels if label == 0]
    if not pos or not neg:
        return 0.5
    # Sum of ranks of positives over all pairs (positive, negative)
    wins = 0.0
    for p in pos:
        for n_ in neg:
            if p > n_:
                wins += 1
            elif p == n_:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def score_v15_current(
    r: Row,
    fame_index_14d: dict[tuple[str, str, date], tuple[int, int]],
) -> float | None:
    """Recompute the V15 (current) popularity score for one row."""
    tier = TEAM_MARKET_TIER.get(r.team_canonical)
    if tier is None:
        return None
    score = {1: 3.0, 2: 2.0, 3: 1.0, 4: 0.0}[tier]

    if r.name_norm in STAR_PLAYER_FLAGS:
        score += 3.0
    elif r.is_pitcher:
        if r.era_at_slate is not None and 0.0 < r.era_at_slate <= LEVERAGE_STAR_PITCHER_ERA:
            score += 2.0
    else:
        if r.ops_at_slate is not None and r.ops_at_slate >= LEVERAGE_STAR_BATTER_OPS:
            score += 2.0

    fame_count, _ = fame_index_14d.get((r.name_norm, r.team_canonical, r.date), (0, 0))
    if fame_count >= 3:
        score += 2.0
    elif fame_count >= 1:
        score += 1.0
    return score


def score_v151_continuous(
    r: Row,
    fame_index_pos: dict[tuple[str, str, date], tuple[int, int]],
) -> float | None:
    """V15.1 continuous score — calls the live `_elite_stat_pts` helper.

    Constants and curve shapes come from `app.core.constants` and
    `app.core.popularity._elite_stat_pts`, so any future tweak to those
    is automatically reflected in the next calibration run.  We do NOT
    call the live `predict_popularity_score` directly because (a) it
    needs the live fame-index file (which would re-derive from the same
    corpus we're walking, redundant), and (b) it raises on missing
    season stats — the calibration walks every row including ones the
    live runtime would route to predict_rookie_popularity_score.  The
    score build below mirrors predict_popularity_score's logic exactly.
    """
    tier = TEAM_MARKET_TIER.get(r.team_canonical)
    if tier is None:
        return None
    score = {1: 3.0, 2: 2.0, 3: 1.0, 4: 0.0}[tier]

    if r.name_norm in STAR_PLAYER_FLAGS:
        score += 3.0
    else:
        # Live elite-stats helper — same code path the runtime executes
        score += _elite_stat_pts(r.is_pitcher, r.ops_at_slate, r.era_at_slate)

    # Fame index: rate-based, continuous in [0, 1] → 0..MAX_PTS
    rate, denom = fame_rate(fame_index_pos, r.name_norm, r.team_canonical, r.date)
    if denom >= 1:
        score += LEVERAGE_FAME_RATE_MAX_PTS * rate
    return score


def validate_model_aucs(rows: list[Row]) -> None:
    print("\n" + "=" * 78)
    print("MODEL COMPARISON: AUC for predicting is_most_popular")
    print("=" * 78)

    fame_14d = build_fame_index(rows, LEVERAGE_FAME_INDEX_DAYS)
    fame_28d = build_fame_index(rows, PITCHER_FAME_INDEX_DAYS)

    # Build position-aware fame for V15.1 (pitchers use 28d, batters use 14d).
    # We do this by computing both and selecting per-row in the score function,
    # but easier to just pass the right index per-row.

    def score_v151_pos_aware(r: Row) -> float | None:
        idx = fame_28d if r.is_pitcher else fame_14d
        return score_v151_continuous(r, idx)

    # Current (V15)
    cur_pairs: list[tuple[float, int]] = []
    new_pairs: list[tuple[float, int]] = []
    for r in rows:
        if r.team_canonical not in TEAM_MARKET_TIER:
            continue
        cur = score_v15_current(r, fame_14d)
        new_ = score_v151_pos_aware(r)
        if cur is None or new_ is None:
            continue
        cur_pairs.append((cur, r.is_most_popular))
        new_pairs.append((new_, r.is_most_popular))

    cur_auc = auc_for_score(cur_pairs)
    new_auc = auc_for_score(new_pairs)

    print(f"V15  (current, binary thresholds)  AUC = {cur_auc:.4f}  n={len(cur_pairs)}")
    print(f"V15.1 (continuous components)       AUC = {new_auc:.4f}  n={len(new_pairs)}")
    delta = new_auc - cur_auc
    print(f"Delta: {delta:+.4f}  ({'IMPROVEMENT' if delta > 0 else 'REGRESSION'})")

    # Per-position breakouts
    for label, predicate in [("Pitchers only", lambda r: r.is_pitcher), ("Batters only", lambda r: not r.is_pitcher)]:
        cur_pos = []
        new_pos = []
        for r in rows:
            if r.team_canonical not in TEAM_MARKET_TIER or not predicate(r):
                continue
            cur = score_v15_current(r, fame_14d)
            new_ = score_v151_pos_aware(r)
            if cur is None or new_ is None:
                continue
            cur_pos.append((cur, r.is_most_popular))
            new_pos.append((new_, r.is_most_popular))
        if cur_pos and new_pos:
            print(f"  {label}: V15 AUC={auc_for_score(cur_pos):.4f}  V15.1 AUC={auc_for_score(new_pos):.4f}  n={len(cur_pos)}")

    # Specific case: Bello vs Elder on 2026-05-05
    print("\n" + "-" * 78)
    print("Spot check — Bello vs Elder on 2026-05-05:")
    print("-" * 78)
    target_date = date(2026, 5, 5)
    for r in rows:
        if r.date == target_date and r.name_norm in ("brayan bello", "kyle elder", "bryce elder"):
            cur = score_v15_current(r, fame_14d)
            new_ = score_v151_pos_aware(r)
            print(f"  {r.name_norm:<20} ({r.team_canonical})  is_pitcher={r.is_pitcher}  era={r.era_at_slate}")
            print(f"    V15 score: {cur}  V15.1 score: {new_:.2f}")


def main() -> int:
    # Step 4: optionally load through the SQLite store rather than direct
    # /data/ reads.  Default is SQLite; BO_HISTORICAL_SOURCE=csv falls back
    # to the on-disk CSVs (transitional, removed in Step 6).
    import tempfile as _tempfile
    import sys as _sys, pathlib as _pathlib
    _repo = _pathlib.Path(__file__).resolve().parents[1]
    if str(_repo) not in _sys.path:
        _sys.path.insert(0, str(_repo))
    from scripts._historical_loader import env_source as _env_source
    _historical_source = _env_source()
    if _historical_source == "sqlite":
        from scripts.export_historical_csvs import export_all as _export_all
        _hist_tmpdir = _tempfile.mkdtemp(prefix="hist_export_")
        _export_all(out_dir=_pathlib.Path(_hist_tmpdir))
        _hist_data_dir = _pathlib.Path(_hist_tmpdir)
    else:
        _hist_data_dir = ROOT / "data"
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-current", action="store_true",
                        help="Compute AUC of current implementation only")
    parser.parse_args()

    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found", file=sys.stderr)
        return 1

    rows = load_rows(CSV_PATH)
    print(f"Loaded {len(rows)} player-slate rows from {CSV_PATH.name}")

    n_pitchers = sum(1 for r in rows if r.is_pitcher)
    n_batters = sum(1 for r in rows if not r.is_pitcher)
    print(f"  Pitchers: {n_pitchers}  Batters: {n_batters}")

    fit_market_tier(rows)

    fit_fame_rate([r for r in rows if not r.is_pitcher],
                  LEVERAGE_FAME_INDEX_DAYS,
                  f"Batters ({LEVERAGE_FAME_INDEX_DAYS}d window)")
    fit_fame_rate([r for r in rows if r.is_pitcher],
                  PITCHER_FAME_INDEX_DAYS,
                  f"Pitchers ({PITCHER_FAME_INDEX_DAYS}d window)")

    fit_era_at_slate(rows)
    fit_ops_at_slate(rows)
    fit_star_flag(rows)

    validate_model_aucs(rows)

    return 0


if __name__ == "__main__":
    sys.exit(main())
