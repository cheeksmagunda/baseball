"""Time-Machine Backtest — Rolling N-1 Condition Matrix Validation

For each date N in the historical dataset, builds the condition matrix using
ONLY data from dates < N (strict N-1 training), then simulates two lineups
for date N and measures HV capture rate.

DATA LEAKAGE CAVEAT (read before interpreting results)
-------------------------------------------------------
A perfectly clean time-machine test requires pre-game signals that were never
stored: Google Trends buzz, ESPN RSS, batting orders, Vegas lines, weather,
starter ERA/K9.  historical_players.csv is post-game leaderboard data only.

The unavoidable proxy we use:
  - FADE:   is_most_popular=1 OR is_most_drafted_3x=1
            (post-game platform flags — correlate strongly with pre-game hype)
  - TARGET: drafts <= 100 AND not FADE
            (draft count is post-game, but players with 1-5 drafts were
             genuinely obscure before the slate — the correlation is tight)
  - NEUTRAL: everything else

What this tests CLEANLY (no leakage):
  - Rolling N-1 condition matrix (built strictly from prior dates)
  - 1P + 4B composition constraint
  - MAX_PLAYERS_PER_TEAM = 1

What this tests via proxy (tight correlation, not perfect):
  - FADE/TARGET/NEUTRAL classification based on crowd signals

Lineup composition: exactly 1 pitcher in Slot 1 + 4 batters in Slots 2-5.
Max 1 player per team per lineup. Starting 5 and Moonshot share 0 players.
"""

import csv
import statistics
from pathlib import Path
from collections import defaultdict

HISTORICAL_PLAYERS_CSV = Path(__file__).resolve().parents[1] / "data" / "historical_players.csv"

# Proxy classification thresholds (match recalibrate_condition_matrix.py)
TARGET_DRAFT_CEILING = 100

# Global matrix fallback (from condition_classifier.py V6.2)
GLOBAL_MATRIX = {
    "batter":  {"TARGET": 1.000, "NEUTRAL": 0.650, "FADE": 0.275},
    "pitcher": {"TARGET": 1.000, "NEUTRAL": 0.850, "FADE": 0.710},
}

PITCHER_POSITIONS = {"P", "SP", "RP"}
MIN_PRIOR_DATES = 2  # need at least this many prior dates for a rolling matrix


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_players(path: Path) -> list[dict]:
    rows = []
    with open(path) as f:
        for row in csv.DictReader(f):
            if not row["real_score"].strip():
                continue  # DNP/scratch
            try:
                row["_rs"] = float(row["real_score"])
            except ValueError:
                continue
            try:
                row["_drafts"] = float(row["drafts"]) if row["drafts"].strip() else 0.0
            except ValueError:
                row["_drafts"] = 0.0
            row["_hv"] = row["is_highest_value"] == "1"
            row["_is_pop"] = row["is_most_popular"] == "1"
            row["_is_3x"] = row["is_most_drafted_3x"] == "1"
            row["_is_pitcher"] = row["position"].strip().upper() in PITCHER_POSITIONS
            rows.append(row)
    return rows


def classify(row: dict) -> str:
    if row["_is_pop"] or row["_is_3x"]:
        return "FADE"
    if row["_drafts"] <= TARGET_DRAFT_CEILING:
        return "TARGET"
    return "NEUTRAL"


def build_rolling_matrix(prior_rows: list[dict]) -> dict:
    """Build condition matrix from prior dates only. Falls back to global if sparse."""
    buckets: dict[str, dict[str, list[float]]] = {
        "batter":  {"TARGET": [], "NEUTRAL": [], "FADE": []},
        "pitcher": {"TARGET": [], "NEUTRAL": [], "FADE": []},
    }
    for r in prior_rows:
        tier = classify(r)
        pos = "pitcher" if r["_is_pitcher"] else "batter"
        buckets[pos][tier].append(r["_rs"])

    matrix: dict[str, dict[str, float]] = {}
    for pos in ("batter", "pitcher"):
        target_vals = buckets[pos]["TARGET"]
        target_avg = statistics.mean(target_vals) if len(target_vals) >= 3 else None
        matrix[pos] = {}
        for tier in ("TARGET", "NEUTRAL", "FADE"):
            if target_avg is None or not buckets[pos][tier]:
                matrix[pos][tier] = GLOBAL_MATRIX[pos][tier]
            elif tier == "TARGET":
                matrix[pos][tier] = 1.000
            else:
                tier_avg = statistics.mean(buckets[pos][tier])
                # Clamp: derived factor must be between 0.05 and 1.50
                matrix[pos][tier] = max(0.05, min(1.50, tier_avg / target_avg))
    return matrix


def sim_ev(row: dict, matrix: dict) -> float:
    """Simulated EV: condition factor (primary) + inverse-draft tiebreaker."""
    tier = classify(row)
    pos = "pitcher" if row["_is_pitcher"] else "batter"
    factor = matrix[pos].get(tier, GLOBAL_MATRIX[pos].get(tier, 0.5))
    # Tiebreaker within tier: lower drafts = higher EV
    draft_tb = 1.0 / (1.0 + row["_drafts"] / 100.0)
    return factor * 100.0 + draft_tb


def pick_lineup(
    candidates: list[dict],
    excluded_names: set[str],
    label: str,
) -> list[dict] | None:
    """
    Pick 1 pitcher + 4 batters from candidates.
    Constraints:
      - Excluded names (Starting 5 players for Moonshot)
      - MAX_PLAYERS_PER_TEAM = 1
    Returns list of 5 players or None if composition impossible.
    """
    pool = [r for r in candidates if r["player_name"] not in excluded_names]
    pool.sort(key=lambda r: r["_sim_ev"], reverse=True)

    # Phase 1: anchor pitcher (highest sim_ev pitcher)
    pitcher = None
    used_teams: set[str] = set()
    for r in pool:
        if r["_is_pitcher"]:
            pitcher = r
            used_teams.add(r["team"])
            break

    if pitcher is None:
        return None

    # Phase 2: fill 4 batters, max 1 per team
    batters: list[dict] = []
    for r in pool:
        if r is pitcher or r["_is_pitcher"]:
            continue
        if r["team"] in used_teams:
            continue
        batters.append(r)
        used_teams.add(r["team"])
        if len(batters) == 4:
            break

    if len(batters) < 4:
        return None  # insufficient diversity

    return [pitcher] + batters


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    all_rows = load_players(HISTORICAL_PLAYERS_CSV)
    dates = sorted({r["date"] for r in all_rows})

    # Group rows by date
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in all_rows:
        by_date[r["date"]].append(r)

    print("TIME-MACHINE BACKTEST — Rolling N-1 Condition Matrix")
    print("=" * 72)
    print("Proxy: is_most_popular/3x → FADE  |  drafts≤100 → TARGET  |  else → NEUTRAL")
    print("Composition: 1 pitcher + 4 batters per lineup | MAX_PLAYERS_PER_TEAM=1")
    print("=" * 72)
    print()

    # Aggregate tracking
    total_s5_hv = total_moon_hv = total_s5_picks = total_moon_picks = 0
    total_hv_available = 0
    skipped_dates = 0
    results = []

    for i, date in enumerate(dates):
        prior_rows = [r for r in all_rows if r["date"] < date]
        prior_dates = sorted({r["date"] for r in prior_rows})

        # Skip if not enough prior data
        if len(prior_dates) < MIN_PRIOR_DATES:
            skipped_dates += 1
            continue

        # Build rolling N-1 matrix
        matrix = build_rolling_matrix(prior_rows)

        # Score day-N candidates
        day_rows = by_date[date]
        for r in day_rows:
            r["_sim_ev"] = sim_ev(r, matrix)

        # Count HV players available on this date
        hv_count = sum(1 for r in day_rows if r["_hv"])

        # Pick Starting 5
        s5 = pick_lineup(day_rows, excluded_names=set(), label="S5")
        if s5 is None:
            skipped_dates += 1
            continue

        s5_names = {r["player_name"] for r in s5}

        # Pick Moonshot (exclude S5 player names)
        moon = pick_lineup(day_rows, excluded_names=s5_names, label="Moon")
        if moon is None:
            skipped_dates += 1
            continue

        # Evaluate
        s5_hv = sum(1 for r in s5 if r["_hv"])
        moon_hv = sum(1 for r in moon if r["_hv"])
        combined_hv = s5_hv + moon_hv

        total_s5_hv += s5_hv
        total_moon_hv += moon_hv
        total_s5_picks += 5
        total_moon_picks += 5
        total_hv_available += hv_count

        # Tier breakdown for the 10 picks
        all_picks = s5 + moon
        tier_counts = {"TARGET": 0, "NEUTRAL": 0, "FADE": 0}
        for r in all_picks:
            tier_counts[classify(r)] += 1

        results.append({
            "date": date,
            "prior_n": len(prior_dates),
            "hv_avail": hv_count,
            "s5_hv": s5_hv,
            "moon_hv": moon_hv,
            "combined_hv": combined_hv,
            "tier_target": tier_counts["TARGET"],
            "tier_neutral": tier_counts["NEUTRAL"],
            "tier_fade": tier_counts["FADE"],
            "s5": s5,
            "moon": moon,
        })

    # --- Per-date table ---
    print(f"{'Date':<12} {'Prior':>5}  {'HV':>4}  {'S5 HV':>6}  {'MN HV':>6}  "
          f"{'10-pick HV':>10}  {'Tgt':>4}  {'Neu':>4}  {'Fad':>4}")
    print("-" * 72)
    for r in results:
        print(
            f"{r['date']:<12} {r['prior_n']:>5}  {r['hv_avail']:>4}  "
            f"{r['s5_hv']}/5  {r['moon_hv']}/5  "
            f"{r['combined_hv']:>3}/10  "
            f"     {r['tier_target']:>4}  {r['tier_neutral']:>4}  {r['tier_fade']:>4}"
        )

    # --- Aggregate summary ---
    n_tested = len(results)
    total_picks = total_s5_picks + total_moon_picks
    total_hv_captured = total_s5_hv + total_moon_hv

    print()
    print("=" * 72)
    print(f"SUMMARY  ({n_tested} dates tested, {skipped_dates} skipped — insufficient prior data)")
    print("-" * 72)
    print(f"  Starting 5 HV capture:  {total_s5_hv}/{total_s5_picks}  "
          f"= {total_s5_hv/total_s5_picks:.1%}")
    print(f"  Moonshot   HV capture:  {total_moon_hv}/{total_moon_picks}  "
          f"= {total_moon_hv/total_moon_picks:.1%}")
    print(f"  Combined   HV capture:  {total_hv_captured}/{total_picks}  "
          f"= {total_hv_captured/total_picks:.1%}")
    print()
    print(f"  Total HV players available across tested dates: {total_hv_available}")
    print(f"  Avg HV available per date: {total_hv_available/n_tested:.1f}")
    print()

    # Per-date hits
    perfect_days = sum(1 for r in results if r["combined_hv"] >= 5)
    strong_days  = sum(1 for r in results if r["combined_hv"] >= 3)
    zero_days    = sum(1 for r in results if r["combined_hv"] == 0)
    print(f"  Days with 5+ HV in 10 picks: {perfect_days}/{n_tested}")
    print(f"  Days with 3+ HV in 10 picks: {strong_days}/{n_tested}")
    print(f"  Days with 0  HV in 10 picks: {zero_days}/{n_tested}")

    print()
    print("DATA LEAKAGE CAVEAT:")
    print("  Draft counts and platform flags are post-game signals used as")
    print("  crowd-attention proxies. True pre-game signals (Google Trends,")
    print("  ESPN RSS, batting orders, Vegas lines) were not stored historically.")
    print("  This tests the CROWD-AVOIDANCE ARCHETYPE SIGNAL in isolation —")
    print("  the live system additionally scores env (Vegas/ERA/park/series)")
    print("  and trait (K/9, ISO, recent form) which are unavailable here.")
    print()

    # --- Sample picks for a few interesting dates ---
    print("=" * 72)
    print("SAMPLE PICKS (first 3 tested dates)")
    print("=" * 72)
    for r in results[:3]:
        print(f"\n{r['date']}  (N-1 matrix trained on {r['prior_n']} prior dates)")
        print(f"  HV available: {r['hv_avail']}")
        print(f"  Starting 5 ({r['s5_hv']}/5 HV):")
        for p in r["s5"]:
            tier = classify(p)
            hv_mark = "★HV" if p["_hv"] else "   "
            print(f"    {hv_mark}  {p['player_name']:<22} {p['team']:<5} "
                  f"{p['position']:<3} {tier:<8} "
                  f"drafts={p['_drafts']:>6.0f}  RS={p['_rs']:>5.1f}")
        print(f"  Moonshot ({r['moon_hv']}/5 HV):")
        for p in r["moon"]:
            tier = classify(p)
            hv_mark = "★HV" if p["_hv"] else "   "
            print(f"    {hv_mark}  {p['player_name']:<22} {p['team']:<5} "
                  f"{p['position']:<3} {tier:<8} "
                  f"drafts={p['_drafts']:>6.0f}  RS={p['_rs']:>5.1f}")


if __name__ == "__main__":
    main()
