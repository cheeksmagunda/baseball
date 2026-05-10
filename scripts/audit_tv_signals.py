"""TV-target signal audit — what pre-game conditions correlate with HIGH TV.

Same bucketing methodology as `audit_hv_hit_rate.py` but with `total_value`
(real_score × (slot_mult + card_boost), as recorded by the platform) as
the response variable instead of `is_highest_value` / `real_score`.

WHY: V15.5 was tuned against HV-hit-rate@5. But "did our 5 picks land
on the HV leaderboard?" is not the same as "did our 5 picks have high
TV?". A contrarian RS=4 player with boost=3 produces TV=20, beating a
star RS=8 player with boost=0 (TV=16). The historical record shows
this dynamic regularly: the leverage discount preferentially keeps
contrarians, who systematically have higher boost. The question this
audit answers is: are env signals also carrying differential TV
correlation distinct from their RS correlation, AND does the
popularity-score axis reproduce the user's thesis that "popular
players need a super-strong RS to overcome their low boost"?

WHAT this prints:
  - Top-line corpus stats (n, mean_RS, mean_TV)
  - Cross-tab: popularity_score quintile × outcome metrics (HV%, RS,
    TV, mean_boost, top-5-TV%, top-5-RS%)
  - "RS needed to reach top-5 TV" stratified by popularity bucket —
    the empirical version of the user's thesis
  - Per-quartile mean_RS / mean_TV / HV% / top-5-TV% for every active
    env signal (batter and pitcher tracks)

CRITICAL RULE: TV (and RS, and card_boost) are used here ONLY as
outcome labels. The runtime never reads them — this script is
output-only and lives in /scripts/ where the audit isolation gate
exempts it. No boost predictor, no slot-ordering heuristic that uses
boost.  The user controls boost during the draft; the platform deals
it.  We measure how pre-game signals correlate with TV outcomes; we do
not feed TV/boost into the model.

Per CLAUDE.md "calibration scripts in /scripts/ may read outcome
columns" — this script does, and writes only to scripts/output/.

Usage:
    BO_CURRENT_SEASON=2026 .venv/bin/python scripts/audit_tv_signals.py
"""

from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict
from datetime import date as DateType
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")

from app.core.constants import canonicalize_team  # noqa: E402
from app.core.popularity import (  # noqa: E402
    predict_popularity_score,
    predict_rookie_popularity_score,
)
from scripts.audit_hv_hit_rate import load_slate_envs  # noqa: E402


PITCHER_POSITIONS = {"P", "SP", "RP"}


def is_pitcher_pos(pos: str) -> bool:
    return (pos or "").strip().upper() in PITCHER_POSITIONS


def _opt_float(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _quartile_breaks(values: list[float]) -> list[float] | None:
    sorted_vals = sorted(v for v in values if v is not None)
    if len(sorted_vals) < 4:
        return None
    n = len(sorted_vals)
    return [sorted_vals[n // 4], sorted_vals[n // 2], sorted_vals[3 * n // 4]]


def _bucket(value: float | None, breaks: list[float] | None) -> str | None:
    if value is None or breaks is None:
        return None
    if value <= breaks[0]:
        return "Q1"
    if value <= breaks[1]:
        return "Q2"
    if value <= breaks[2]:
        return "Q3"
    return "Q4"


def _pop_bucket(score: float) -> str:
    if score < 2.0:
        return "0-2  (max sleeper)"
    if score < 4.0:
        return "2-4  (mild sleeper)"
    if score < 6.0:
        return "4-6  (mid-fame)"
    if score < 8.0:
        return "6-8  (popular)"
    return "8+   (max consensus)"


POP_BUCKET_ORDER = [
    "0-2  (max sleeper)",
    "2-4  (mild sleeper)",
    "4-6  (mid-fame)",
    "6-8  (popular)",
    "8+   (max consensus)",
]


def load_records(historical_csv: Path, slate_envs: dict) -> list[dict]:
    """Build enriched per-player records joined with slate env conditions."""
    by_date: dict[str, list[dict]] = defaultdict(list)
    with historical_csv.open() as f:
        for row in csv.DictReader(f):
            by_date[row["date"]].append(row)

    out: list[dict] = []
    for date_str in sorted(by_date):
        if date_str not in slate_envs:
            continue
        env_lookup = slate_envs[date_str]
        as_of = DateType.fromisoformat(date_str)
        for row in by_date[date_str]:
            team = canonicalize_team(row["team"])
            if team not in env_lookup:
                continue
            game, is_home = env_lookup[team]
            side = "home" if is_home else "away"
            other = "away" if is_home else "home"

            rs = _opt_float(row["real_score"])
            # total_value derived inline as rs * (2 + cb) per CLAUDE.md — the
            # standalone CSV column was dropped in the May 2026 cleanup sweep.
            cb = _opt_float(row.get("card_boost"))
            tv = (rs * (2 + (cb or 0))) if rs is not None else None
            if rs is None or tv is None:
                continue  # DNP/scratch — no outcome label

            pos = row["position"]
            is_p = is_pitcher_pos(pos)
            season_ops = _opt_float(row.get("ops_at_slate"))
            season_era = _opt_float(row.get("era_at_slate"))
            is_rookie = season_era is None if is_p else season_ops is None

            try:
                if is_rookie:
                    pop = predict_rookie_popularity_score(
                        player_name=row["player_name"],
                        team=team,
                        is_pitcher=is_p,
                        batting_order=5,
                        as_of=as_of,
                    )
                elif is_p:
                    pop = predict_popularity_score(
                        player_name=row["player_name"],
                        team=team,
                        is_pitcher=True,
                        batting_order=None,
                        season_ops=None,
                        season_era=season_era,
                        as_of=as_of,
                    )
                else:
                    pop = predict_popularity_score(
                        player_name=row["player_name"],
                        team=team,
                        is_pitcher=False,
                        batting_order=5,
                        season_ops=season_ops,
                        season_era=None,
                        as_of=as_of,
                    )
            except Exception:
                continue

            # boost = TV / RS - 2 — derived from the platform's recorded values.
            # Used here as an OUTCOME label for the "popular players need
            # high RS to overcome low boost" cross-tab. NEVER fed back into
            # the model — see module docstring.
            implied_boost = (tv / rs - 2.0) if rs > 0 else None

            out.append({
                "date": date_str,
                "name": row["player_name"],
                "team": team,
                "position": pos,
                "is_pitcher": is_p,
                "is_rookie": is_rookie,
                "rs": rs,
                "tv": tv,
                "boost": implied_boost,
                "is_hv": int(row["is_highest_value"] or 0),
                "is_mp": int(row["is_most_popular"] or 0),
                "season_ops": season_ops,
                "season_era": season_era,
                "pop_score": pop,
                # batter-side env signals
                "opp_starter_era": game.get(f"{other}_starter_era"),
                "opp_starter_whip": game.get(f"{other}_starter_whip"),
                "opp_starter_k_per_9": game.get(f"{other}_starter_k_per_9"),
                "vegas_total": game.get("vegas_total"),
                "own_moneyline": game.get(f"{side}_moneyline"),
                "wind_speed": game.get("wind_speed_mph"),
                "wind_direction": game.get("wind_direction"),
                "temperature": game.get("temperature_f"),
                # pitcher-side env signals
                "opp_team_ops": game.get(f"{other}_team_ops"),
                "own_starter_k_per_9": game.get(f"{side}_starter_k_per_9"),
                "own_starter_era": game.get(f"{side}_starter_era"),
            })

    # Per-slate ranks (used for top-K capture rate metrics)
    by_d: dict[str, list[dict]] = defaultdict(list)
    for r in out:
        by_d[r["date"]].append(r)
    for recs in by_d.values():
        for rank, r in enumerate(sorted(recs, key=lambda x: -x["tv"]), start=1):
            r["tv_rank"] = rank
        for rank, r in enumerate(sorted(recs, key=lambda x: -x["rs"]), start=1):
            r["rs_rank"] = rank
    return out


def _agg(recs: list[dict]) -> dict:
    n = len(recs)
    if n == 0:
        return {}
    boosts = [r["boost"] for r in recs if r["boost"] is not None]
    return {
        "n": n,
        "mean_rs": sum(r["rs"] for r in recs) / n,
        "mean_tv": sum(r["tv"] for r in recs) / n,
        "mean_boost": (sum(boosts) / len(boosts)) if boosts else 0.0,
        "hv_rate": sum(r["is_hv"] for r in recs) / n,
        "top5_tv_rate": sum(1 for r in recs if r["tv_rank"] <= 5) / n,
        "top10_tv_rate": sum(1 for r in recs if r["tv_rank"] <= 10) / n,
        "top5_rs_rate": sum(1 for r in recs if r["rs_rank"] <= 5) / n,
    }


def report_popularity_crosstab(records: list[dict]) -> None:
    print("\n=== Popularity score × outcome metrics ===")
    print("(low pop → contrarian; high pop → consensus pick)\n")
    print(
        f"{'pop_bucket':<22} {'n':>4} {'rs':>6} {'tv':>6} {'boost':>6}  "
        f"{'HV%':>5} {'top5_TV%':>9} {'top5_RS%':>9}"
    )
    by_b: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if r["pop_score"] is not None:
            by_b[_pop_bucket(r["pop_score"])].append(r)
    for b in POP_BUCKET_ORDER:
        recs = by_b.get(b, [])
        if not recs:
            continue
        a = _agg(recs)
        print(
            f"{b:<22} {a['n']:>4d} {a['mean_rs']:>6.2f} {a['mean_tv']:>6.2f} "
            f"{a['mean_boost']:>6.2f}  {a['hv_rate']*100:>4.1f}% "
            f"{a['top5_tv_rate']*100:>8.1f}% {a['top5_rs_rate']*100:>8.1f}%"
        )


def report_rs_floor_for_top_tv(records: list[dict]) -> None:
    """Among players who landed top-5 TV, what RS did they need by pop bucket?

    Validates user's thesis: popular players need a super-strong RS to
    overcome their low boost.
    """
    top5 = [r for r in records if r["tv_rank"] <= 5]
    print("\n=== RS needed to reach top-5 TV, stratified by popularity bucket ===")
    print(
        f"{'pop_bucket':<22} {'n':>4} {'mean_rs':>8} {'min_rs':>7} "
        f"{'mean_boost':>11} {'mean_tv':>8}"
    )
    by_b: dict[str, list[dict]] = defaultdict(list)
    for r in top5:
        if r["pop_score"] is not None:
            by_b[_pop_bucket(r["pop_score"])].append(r)
    for b in POP_BUCKET_ORDER:
        recs = by_b.get(b, [])
        if not recs:
            continue
        rs_vals = [r["rs"] for r in recs]
        boosts = [r["boost"] for r in recs if r["boost"] is not None]
        tv_vals = [r["tv"] for r in recs]
        print(
            f"{b:<22} {len(recs):>4d} {sum(rs_vals)/len(rs_vals):>8.2f} "
            f"{min(rs_vals):>7.2f} "
            f"{(sum(boosts)/len(boosts) if boosts else 0):>11.2f} "
            f"{sum(tv_vals)/len(tv_vals):>8.2f}"
        )


def report_signal(
    records: list[dict],
    signal_field: str,
    label: str,
    *,
    batter_only: bool = False,
    pitcher_only: bool = False,
) -> None:
    pool = [r for r in records if r[signal_field] is not None]
    if batter_only:
        pool = [r for r in pool if not r["is_pitcher"]]
    if pitcher_only:
        pool = [r for r in pool if r["is_pitcher"]]
    if len(pool) < 8:
        print(f"\n-- {label}: too few rows ({len(pool)}) --")
        return
    breaks = _quartile_breaks([r[signal_field] for r in pool])
    if breaks is None:
        return
    by_b: dict[str, list[dict]] = defaultdict(list)
    for r in pool:
        b = _bucket(r[signal_field], breaks)
        if b:
            by_b[b].append(r)

    print(f"\n=== {label}  (breaks: ≤{breaks[0]:.2f}, ≤{breaks[1]:.2f}, ≤{breaks[2]:.2f}) ===")
    print(
        f"{'bucket':<6} {'n':>4} {'rs':>6} {'tv':>6} {'HV%':>6} {'top5_TV%':>9} {'top10_TV%':>10}"
    )
    for b in ("Q1", "Q2", "Q3", "Q4"):
        recs = by_b.get(b, [])
        if not recs:
            continue
        a = _agg(recs)
        print(
            f"{b:<6} {a['n']:>4d} {a['mean_rs']:>6.2f} {a['mean_tv']:>6.2f} "
            f"{a['hv_rate']*100:>5.1f}% {a['top5_tv_rate']*100:>8.1f}% "
            f"{a['top10_tv_rate']*100:>9.1f}%"
        )


def write_records_csv(records: list[dict], path: Path) -> None:
    if not records:
        return
    fields = [
        "date", "name", "team", "position", "is_pitcher", "is_rookie",
        "rs", "tv", "boost", "is_hv", "is_mp",
        "tv_rank", "rs_rank", "pop_score",
        "opp_starter_era", "opp_starter_whip", "opp_starter_k_per_9",
        "vegas_total", "own_moneyline",
        "wind_speed", "wind_direction", "temperature",
        "opp_team_ops", "own_starter_k_per_9", "own_starter_era",
        "season_ops", "season_era",
    ]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k) for k in fields})


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
    historical_csv = _hist_data_dir / "historical_players.csv"
    slate_results = _hist_data_dir / "historical_slate_results.json"
    output_dir = ROOT / "scripts" / "output"
    output_dir.mkdir(exist_ok=True)
    out_csv = output_dir / "tv_signal_records.csv"

    slate_envs = load_slate_envs(slate_results)
    records = load_records(historical_csv, slate_envs)
    if not records:
        print("No records loaded — check that historical_slate_results.json has env data")
        return 1

    n = len(records)
    n_dates = len(set(r["date"] for r in records))
    n_p = sum(1 for r in records if r["is_pitcher"])
    print(f"Loaded {n} records ({n - n_p} batters, {n_p} pitchers) "
          f"from {n_dates} slates")
    print(f"  pool HV-rate:  {sum(r['is_hv'] for r in records) / n * 100:>5.1f}%")
    print(f"  pool MP-rate:  {sum(r['is_mp'] for r in records) / n * 100:>5.1f}%")
    print(f"  mean RS:       {sum(r['rs'] for r in records) / n:>5.2f}")
    print(f"  mean TV:       {sum(r['tv'] for r in records) / n:>5.2f}")

    report_popularity_crosstab(records)
    report_rs_floor_for_top_tv(records)

    print("\n\n========== BATTER ENV SIGNALS  (TV-target view) ==========")
    report_signal(records, "opp_starter_era", "Opp starter ERA", batter_only=True)
    report_signal(records, "opp_starter_whip", "Opp starter WHIP", batter_only=True)
    report_signal(records, "opp_starter_k_per_9", "Opp starter K/9", batter_only=True)
    report_signal(records, "vegas_total", "Vegas O/U", batter_only=True)
    report_signal(records, "own_moneyline", "Own moneyline", batter_only=True)
    report_signal(records, "wind_speed", "Wind speed (mph)", batter_only=True)
    report_signal(records, "temperature", "Temperature (F)", batter_only=True)

    print("\n\n========== PITCHER ENV SIGNALS  (TV-target view) ==========")
    report_signal(records, "vegas_total", "Vegas O/U (pitcher)", pitcher_only=True)
    report_signal(records, "own_moneyline", "Own moneyline (pitcher)", pitcher_only=True)
    report_signal(records, "own_starter_k_per_9", "Own K/9 (pitcher)", pitcher_only=True)
    report_signal(records, "own_starter_era", "Own ERA (pitcher)", pitcher_only=True)
    report_signal(records, "opp_team_ops", "Opp team OPS (pitcher)", pitcher_only=True)

    write_records_csv(records, out_csv)
    print(f"\nFull records CSV: {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
