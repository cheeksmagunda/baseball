"""Threshold drift report — Claude-in-the-loop calibration brief.

Reads historical outcomes (historical_players.csv) and conditions
(historical_conditions.csv), buckets each env threshold in app/core/constants.py,
and emits a markdown report flagging thresholds whose below/mid/above Highest-
Value hit rates are weak, inverted, or based on sparse data.

The pipeline NEVER reads this report or the historical CSVs at runtime.  This
is strictly a Claude/operator-facing analysis that informs future manual edits
to constants.py.

Usage:
    python scripts/threshold_drift_report.py                # stdout
    python scripts/threshold_drift_report.py -o report.md   # to file

Reuses the bucket logic from scripts/calibrate_env_scoring.py.  The difference:
this script produces a structured markdown summary instead of free-form stdout,
and it explicitly flags which thresholds are candidates for adjustment.
"""

import argparse
import csv
import sys
from pathlib import Path
from statistics import mean

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
PLAYERS_CSV = DATA_DIR / "historical_players.csv"
CONDITIONS_CSV = DATA_DIR / "historical_conditions.csv"

MIN_N_FOR_CONFIDENCE = 20
HV_RATE_WEAK_SEPARATION = 0.05   # above_hv - below_hv must exceed this to be "strong"

# (label, field, floor, ceiling, ascending, constant_name, is_pitcher)
THRESHOLDS = [
    ("Vegas O/U",       "vegas_total",     7.0,   9.5,   True,  "BATTER_ENV_VEGAS_FLOOR/CEILING", False),
    ("Opp Starter ERA", "opp_starter_era", 3.5,   5.5,   True,  "BATTER_ENV_ERA_FLOOR/CEILING", False),
    ("Opp Bullpen ERA", "opp_bullpen_era", 3.5,   5.5,   True,  "BATTER_ENV_BULLPEN_ERA_FLOOR/CEILING", False),
    ("Opp Team OPS",    "opp_team_ops",    0.650, 0.780, False, "PITCHER_ENV_OPS_FLOOR/CEILING", True),
    ("Opp Team K%",     "opp_team_k_pct",  0.20,  0.26,  True,  "PITCHER_ENV_K_PCT_FLOOR/CEILING", True),
    ("Pitcher K/9",     "pitcher_k9",      6.0,   10.0,  True,  "PITCHER_ENV_K9_FLOOR/CEILING", True),
]


def _f(val: str) -> float | None:
    if not val or val.strip() == "":
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _load_conditions() -> dict[tuple[str, str], dict]:
    if not CONDITIONS_CSV.exists():
        return {}
    index: dict[tuple[str, str], dict] = {}
    with CONDITIONS_CSV.open() as f:
        for row in csv.DictReader(f):
            d, home, away = row["date"], row["home_team"], row["away_team"]
            index[(d, home)] = {
                "vegas_total":     _f(row["vegas_total"]),
                "opp_starter_era": _f(row["away_starter_era"]),
                "pitcher_k9":      _f(row["home_starter_k9"]),
                "opp_team_ops":    _f(row["away_team_ops"]),
                "opp_team_k_pct":  _f(row["away_team_k_pct"]),
                "opp_bullpen_era": _f(row["away_bullpen_era"]),
            }
            index[(d, away)] = {
                "vegas_total":     _f(row["vegas_total"]),
                "opp_starter_era": _f(row["home_starter_era"]),
                "pitcher_k9":      _f(row["away_starter_k9"]),
                "opp_team_ops":    _f(row["home_team_ops"]),
                "opp_team_k_pct":  _f(row["home_team_k_pct"]),
                "opp_bullpen_era": _f(row["home_bullpen_era"]),
            }
    return index


def _load_players() -> list[dict]:
    if not PLAYERS_CSV.exists():
        return []
    with PLAYERS_CSV.open() as f:
        return [r for r in csv.DictReader(f) if r["real_score"].strip() not in ("", "None")]


def _bucket_stats(
    field: str,
    floor: float,
    ceiling: float,
    ascending: bool,
    is_pitcher: bool,
    players: list[dict],
    conditions: dict[tuple[str, str], dict],
) -> dict[str, tuple[int, float, float]]:
    """Return {bucket: (n, avg_rs, hv_rate)} for the three buckets: below/mid/above."""
    buckets: dict[str, tuple[list[float], list[int]]] = {
        "below": ([], []), "mid": ([], []), "above": ([], []),
    }
    for row in players:
        pos = row["position"].strip().upper()
        if (pos in ("P", "SP", "RP")) != is_pitcher:
            continue
        cond = conditions.get((row["date"], row["team"]))
        if not cond:
            continue
        val = cond.get(field)
        if val is None:
            continue
        rs = float(row["real_score"])
        hv = 1 if row["is_highest_value"] == "1" else 0

        if val <= floor:
            bkt = "below"
        elif val >= ceiling:
            bkt = "above"
        else:
            bkt = "mid"
        buckets[bkt][0].append(rs)
        buckets[bkt][1].append(hv)

    out: dict[str, tuple[int, float, float]] = {}
    for name, (rs_vals, hv_vals) in buckets.items():
        n = len(rs_vals)
        out[name] = (n, mean(rs_vals) if n else 0.0, (sum(hv_vals) / n) if n else 0.0)

    if not ascending:
        # "below" means below floor, which in a descending metric is the "good" bucket.
        # Swap labels so "above" is always the predicted-high-HV bucket.
        out = {"below": out["above"], "mid": out["mid"], "above": out["below"]}
    return out


def _classify_threshold(stats: dict[str, tuple[int, float, float]]) -> tuple[str, str]:
    """Return (status, note) where status ∈ {OK, WEAK, INVERTED, SPARSE}."""
    below_n, _, below_hv = stats["below"]
    mid_n, _, _ = stats["mid"]
    above_n, _, above_hv = stats["above"]
    total = below_n + mid_n + above_n

    if total < MIN_N_FOR_CONFIDENCE:
        return "SPARSE", f"n={total} (<{MIN_N_FOR_CONFIDENCE}) — insufficient data for a verdict"
    if above_n == 0 or below_n == 0:
        return "SPARSE", f"below_n={below_n}, above_n={above_n} — one end of the range is empty"
    if above_hv < below_hv:
        return "INVERTED", f"above-bucket HV={above_hv:.1%} < below-bucket HV={below_hv:.1%} — threshold may be inverted"
    if above_hv - below_hv < HV_RATE_WEAK_SEPARATION:
        return "WEAK", f"separation {above_hv - below_hv:+.1%} below {HV_RATE_WEAK_SEPARATION:+.0%} — threshold poorly discriminative"
    return "OK", f"separation {above_hv - below_hv:+.1%} (above {above_hv:.1%} vs below {below_hv:.1%})"


def _render_markdown(rows: list[tuple[str, str, str, dict]]) -> str:
    out: list[str] = [
        "# Env Threshold Drift Report",
        "",
        f"Samples from `{PLAYERS_CSV.name}` + `{CONDITIONS_CSV.name}`.",
        f"Buckets: below floor / mid / above ceiling.  HV = Highest-Value rate.",
        "",
        "## Summary",
        "",
        "| Threshold | Constant | Status | Verdict |",
        "|---|---|---|---|",
    ]
    for label, constant, status, stats in rows:
        _, verdict = _classify_threshold(stats)
        out.append(f"| {label} | `{constant}` | **{status}** | {verdict} |")
    out.append("")
    out.append("## Per-threshold detail")
    out.append("")
    for label, constant, status, stats in rows:
        out.append(f"### {label}  [`{constant}`] — {status}")
        out.append("")
        out.append("| Bucket | n | avg RS | HV rate |")
        out.append("|---|---:|---:|---:|")
        for bkt in ("below", "mid", "above"):
            n, avg_rs, hv = stats[bkt]
            out.append(f"| {bkt} | {n} | {avg_rs:.2f} | {hv:.1%} |")
        out.append("")
    out.append("## How to use this report")
    out.append("")
    out.append("- **OK**: threshold is cleanly discriminative — no action needed.")
    out.append("- **WEAK**: HV-rate separation between above and below buckets is <5%.")
    out.append("  Consider moving the floor/ceiling closer to the mid band or replacing the factor.")
    out.append("- **INVERTED**: above-bucket HV rate is *lower* than below-bucket — the factor is")
    out.append("  predicting the wrong direction.  Either flip the floor/ceiling or retire the factor.")
    out.append("- **SPARSE**: too few samples to judge.  Keep the threshold, revisit after more slates.")
    out.append("")
    out.append("**No automated tuning.** Edit `app/core/constants.py` manually based on this report.")
    out.append("")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-o", "--output", type=Path, help="write report to file instead of stdout")
    args = parser.parse_args(argv)

    conditions = _load_conditions()
    players = _load_players()

    if not conditions:
        print(f"No conditions loaded from {CONDITIONS_CSV}.  Run scripts/export_slate_conditions.py first.", file=sys.stderr)
        return 1
    if not players:
        print(f"No players loaded from {PLAYERS_CSV}.", file=sys.stderr)
        return 1

    rows: list[tuple[str, str, str, dict]] = []
    for label, field, floor, ceiling, ascending, constant, is_pitcher in THRESHOLDS:
        stats = _bucket_stats(field, floor, ceiling, ascending, is_pitcher, players, conditions)
        status, _ = _classify_threshold(stats)
        rows.append((label, constant, status, stats))

    report = _render_markdown(rows)

    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
