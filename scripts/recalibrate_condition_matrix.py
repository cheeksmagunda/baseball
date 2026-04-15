"""Backtest & recalibrate the V6.0 RS_CONDITION_MATRIX from historical data.

Replaces the V3/V4 script (ownership_tier × boost_tier) — that matrix is dead
code since V6.0 rekeyed everything on (popularity_class × position_type).

Classification proxy
--------------------
The real FADE/TARGET/NEUTRAL labels come from pre-game web scraping
(Google Trends, ESPN RSS, Reddit).  Historical CSVs don't store those labels,
so we proxy using post-game leaderboard flags:

  FADE    → is_most_popular=1  OR  is_most_drafted_3x=1
              (high crowd attention: appears on volume leaderboards)
  TARGET  → drafts ≤ 100  AND  not FADE
              (ghost tier: under the radar)
  NEUTRAL → everything else (100 < drafts, not on a crowd leaderboard)

Known selection bias: historical_players.csv captures leaderboard players
only (Most Popular, Most Drafted 3x, Highest Value).  TARGET batters in this
dataset are almost exclusively HV captures, so their HV rate and avg RS are
biased upward.  FADE numbers are not biased (the full Most Popular leaderboard
is captured).  NEUTRAL suffers mild upward bias (must have appeared on some
leaderboard to be in the dataset).

Usage
-----
    python scripts/recalibrate_condition_matrix.py

Output: backtest table + current-vs-data comparison + paste-ready calibration
values.  DOES NOT write to condition_classifier.py automatically.
"""

import csv
import statistics
from pathlib import Path

HISTORICAL_PLAYERS_CSV = Path(__file__).resolve().parents[1] / "data" / "historical_players.csv"

# Proxy thresholds
FADE_DRAFT_THRESHOLD = 100  # drafts > this AND flagged → FADE
TARGET_DRAFT_CEILING = 100  # drafts ≤ this AND not FADE → TARGET

# Success metric: RS > 3.0  (consistent with RS_CONDITION_OBSERVATIONS)
RS_SUCCESS_THRESHOLD = 3.0

# Current matrix values (from condition_classifier.py V6.1)
CURRENT_MATRIX = {
    "batter": {"TARGET": 1.000, "NEUTRAL": 0.650, "FADE": 0.275},
    "pitcher": {"TARGET": 1.000, "NEUTRAL": 0.850, "FADE": 0.710},
}


def classify(row: dict) -> str:
    """Proxy-classify a player as FADE / TARGET / NEUTRAL."""
    is_pop = row["is_most_popular"] == "1"
    is_3x = row["is_most_drafted_3x"] == "1"
    try:
        drafts = float(row["drafts"]) if row["drafts"].strip() else 0.0
    except ValueError:
        drafts = 0.0

    if is_pop or is_3x:
        return "FADE"
    if drafts <= TARGET_DRAFT_CEILING:
        return "TARGET"
    return "NEUTRAL"


def is_pitcher(row: dict) -> bool:
    return row["position"].strip().upper() in {"P", "SP", "RP"}


def main() -> None:
    rows = list(csv.DictReader(open(HISTORICAL_PLAYERS_CSV)))

    # Drop DNP rows (no real_score)
    valid = [
        r for r in rows
        if r["real_score"].strip() and r["real_score"].strip() not in ("", "None")
    ]

    dates = sorted({r["date"] for r in valid})
    print(f"Dataset: {len(valid)} valid player-appearances across {len(dates)} dates")
    print(f"Dates: {dates[0]} → {dates[-1]}\n")

    # Accumulate stats per (popularity_tier, position_type)
    # Structure: data[pop_class][pos_type] = {rs_vals, hv_vals, success_count, n}
    pop_classes = ["TARGET", "NEUTRAL", "FADE"]
    pos_types = ["batter", "pitcher"]

    buckets: dict[str, dict[str, dict]] = {
        pc: {
            pt: {"rs": [], "hv": [], "success": 0, "n": 0}
            for pt in pos_types
        }
        for pc in pop_classes
    }

    for r in valid:
        pop_class = classify(r)
        pos_type = "pitcher" if is_pitcher(r) else "batter"
        rs = float(r["real_score"])
        hv = r["is_highest_value"] == "1"
        success = rs > RS_SUCCESS_THRESHOLD

        b = buckets[pop_class][pos_type]
        b["rs"].append(rs)
        b["hv"].append(1 if hv else 0)
        b["n"] += 1
        if success:
            b["success"] += 1

    # --- Backtest table ---
    print("=" * 75)
    print(f"{'Tier':<10} {'Type':<8} {'n':>5}  {'avg RS':>7}  {'HV%':>6}  "
          f"{'RS>3%':>6}  {'Curr factor':>12}  {'Status'}")
    print("-" * 75)

    recommendations: dict[str, dict[str, dict]] = {}

    for pt in pos_types:
        for pc in pop_classes:
            b = buckets[pc][pt]
            if b["n"] == 0:
                continue
            avg_rs = statistics.mean(b["rs"])
            hv_rate = sum(b["hv"]) / b["n"]
            rs_gt3_rate = b["success"] / b["n"]
            curr = CURRENT_MATRIX[pt][pc]

            # Derive implied factor relative to TARGET for same position type
            target_avg = statistics.mean(buckets["TARGET"][pt]["rs"]) if buckets["TARGET"][pt]["n"] else 1.0
            implied_factor = avg_rs / target_avg if target_avg > 0 else 0.0

            status = "✓ OK"
            if pc == "NEUTRAL" and b["n"] < 30:
                status = f"⚠ n={b['n']} too sparse"
            elif pc == "NEUTRAL" and abs(implied_factor - curr) > 0.08:
                status = f"⚠ data→{implied_factor:.3f}"

            print(
                f"{pc:<10} {pt:<8} {b['n']:>5}  {avg_rs:>7.3f}  "
                f"{hv_rate:>6.1%}  {rs_gt3_rate:>6.1%}  {curr:>12.3f}  {status}"
            )

            recommendations.setdefault(pt, {})[pc] = {
                "n": b["n"],
                "successes": b["success"],
                "avg_rs": avg_rs,
                "hv_rate": hv_rate,
                "rs_gt3_rate": rs_gt3_rate,
                "implied_factor": implied_factor,
                "curr_factor": curr,
            }
        print()

    # --- Selection-bias note ---
    print("SELECTION BIAS NOTE:")
    print("  FADE:    reliable — full Most Popular + Most Drafted 3x leaderboards are captured.")
    print("  TARGET:  biased ↑ — low-draft players appear only because they hit HV leaderboard.")
    print("  NEUTRAL: biased ↑ — appears only when on some leaderboard (HV captures).")
    print("  NEUTRAL n is sparse because players with 100–500 drafts who did NOT hit HV")
    print("  are not recorded in the CSV (not on any leaderboard). Only once the full")
    print("  slate player pool is stored can NEUTRAL be calibrated from this dataset.")

    # --- Paste-ready calibration output ---
    print("\n" + "=" * 75)
    print("PASTE-READY: RS_CONDITION_MATRIX (V6.2)")
    print("=" * 75)
    print("RS_CONDITION_MATRIX: dict[str, dict[str, float]] = {")
    for pt in pos_types:
        print(f'    "{pt}": {{')
        target_avg = recommendations[pt]["TARGET"]["avg_rs"]
        for pc in pop_classes:
            rec = recommendations[pt][pc]
            impl = rec["implied_factor"]
            curr = rec["curr_factor"]
            comment = f"data-implied {impl:.3f} (n={rec['n']})"
            print(f'        "{pc}": {curr:.3f},   # {comment}')
        print("    },")
    print("}")

    print("\nPASTE-READY: RS_CONDITION_OBSERVATIONS (V6.2)")
    print("RS_CONDITION_OBSERVATIONS: dict[str, dict[str, tuple[int, int]]] = {")
    for pt in pos_types:
        print(f'    "{pt}": {{')
        for pc in pop_classes:
            rec = recommendations[pt][pc]
            s, t = rec["successes"], rec["n"]
            bias_note = " (selection-biased↑)" if pc in ("TARGET", "NEUTRAL") else ""
            print(f'        "{pc}": ({s:3d}, {t:3d}),   # {s/t*100:.1f}% RS>3{bias_note}')
        print("    },")
    print("}")

    # --- Per-date breakdown ---
    print("\n" + "=" * 75)
    print("PER-DATE: TARGET batter HV rate (early-season sanity check)")
    print("-" * 75)
    date_buckets: dict[str, dict] = {}
    for r in valid:
        if is_pitcher(r):
            continue
        d = r["date"]
        pc = classify(r)
        if d not in date_buckets:
            date_buckets[d] = {p: {"hv": [], "n": 0} for p in pop_classes}
        hv = 1 if r["is_highest_value"] == "1" else 0
        date_buckets[d][pc]["hv"].append(hv)
        date_buckets[d][pc]["n"] += 1

    for d in sorted(date_buckets.keys()):
        target = date_buckets[d]["TARGET"]
        fade = date_buckets[d]["FADE"]
        neutral = date_buckets[d]["NEUTRAL"]
        t_hv = f"{sum(target['hv'])/target['n']:.0%}" if target["n"] else "—"
        f_hv = f"{sum(fade['hv'])/fade['n']:.0%}" if fade["n"] else "—"
        n_hv = f"{sum(neutral['hv'])/neutral['n']:.0%}" if neutral["n"] else "—"
        print(f"  {d}  TARGET HV={t_hv} (n={target['n']})  "
              f"NEUTRAL HV={n_hv} (n={neutral['n']})  "
              f"FADE HV={f_hv} (n={fade['n']})")


if __name__ == "__main__":
    main()
