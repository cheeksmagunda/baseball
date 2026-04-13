"""Recalibrate CONDITION_MATRIX and PITCHER_CONDITION_MATRIX from historical data.

Reads data/historical_players.csv and computes empirical HV rates per
(ownership_tier × boost_tier) cell, separately for batters and pitchers.

Uses the same tier thresholds as the fallback path in
app/services/condition_classifier.get_ownership_tier (absolute draft counts)
since historical_players.csv is a curated notable-player sample that lacks
the full slate distribution for percentile calibration.

HV definition: is_highest_value == 1 in historical_players.csv (this is the
label the optimizer is optimizing to hit).

Output: prints new CONDITION_MATRIX, PITCHER_CONDITION_MATRIX, and
corresponding _OBSERVATIONS dicts ready to paste into condition_classifier.py.
"""
import csv
from collections import defaultdict
from pathlib import Path

HISTORICAL_PLAYERS_CSV = Path(__file__).resolve().parents[1] / "data" / "historical_players.csv"

# Tier thresholds — match the fallback branch of
# app/services/condition_classifier.get_ownership_tier
GHOST_ABSOLUTE_DRAFT_FLOOR = 25
GHOST_DRAFT_THRESHOLD = 100
LOW_DRAFT_THRESHOLD = 200
CHALK_DRAFT_THRESHOLD = 1500
MEGA_CHALK_DRAFT_THRESHOLD = 2000

BAYESIAN_PRIOR_ALPHA = 1.0
BAYESIAN_PRIOR_BETA = 1.0

OWNERSHIP_TIERS = ["ghost", "low", "medium", "chalk", "mega_chalk"]
BOOST_TIERS = ["no_boost", "low_boost", "mid_boost", "elite_boost", "max_boost"]


def get_ownership_tier(drafts: int) -> str:
    if drafts <= GHOST_ABSOLUTE_DRAFT_FLOOR:
        return "ghost"
    if drafts < GHOST_DRAFT_THRESHOLD:
        return "ghost"
    if drafts < LOW_DRAFT_THRESHOLD:
        return "low"
    if drafts < CHALK_DRAFT_THRESHOLD:
        return "medium"
    if drafts < MEGA_CHALK_DRAFT_THRESHOLD:
        return "chalk"
    return "mega_chalk"


def get_boost_tier(card_boost: float) -> str:
    if card_boost < 1.0:
        return "no_boost"
    if card_boost < 2.0:
        return "low_boost"
    if card_boost < 2.5:
        return "mid_boost"
    if card_boost < 3.0:
        return "elite_boost"
    return "max_boost"


def bayesian_rate(successes: int, trials: int) -> float:
    return (successes + BAYESIAN_PRIOR_ALPHA) / (trials + BAYESIAN_PRIOR_ALPHA + BAYESIAN_PRIOR_BETA)


def interpolate_boost_row(row: dict[str, tuple[int, int]]) -> dict[str, float]:
    """For cells with zero observations, interpolate from neighboring boost tiers
    on the same ownership row using monotone-preserving linear fill.
    """
    # Compute bayesian rate per tier; mark no-data cells as None for interpolation
    rates: dict[str, float | None] = {}
    for bt in BOOST_TIERS:
        s, t = row.get(bt, (0, 0))
        rates[bt] = bayesian_rate(s, t) if t > 0 else None

    # Forward-fill + back-fill + linear interpolate for Nones
    indexed = [rates[bt] for bt in BOOST_TIERS]
    n = len(indexed)
    # Find anchor indices where we have data
    anchors = [i for i, v in enumerate(indexed) if v is not None]
    if not anchors:
        # No data at all — uniform Beta(1,1) prior mean
        return {bt: 0.5 for bt in BOOST_TIERS}
    filled = indexed.copy()
    # Fill before first anchor
    for i in range(anchors[0]):
        filled[i] = indexed[anchors[0]]
    # Fill after last anchor
    for i in range(anchors[-1] + 1, n):
        filled[i] = indexed[anchors[-1]]
    # Linear interpolation between adjacent anchors
    for a, b in zip(anchors, anchors[1:]):
        if b - a <= 1:
            continue
        lo, hi = indexed[a], indexed[b]
        for i in range(a + 1, b):
            frac = (i - a) / (b - a)
            filled[i] = lo + (hi - lo) * frac
    return {bt: round(filled[i], 3) for i, bt in enumerate(BOOST_TIERS)}


def main() -> None:
    rows = list(csv.DictReader(open(HISTORICAL_PLAYERS_CSV)))
    # Accumulate (successes, trials) per (is_pitcher, ownership_tier, boost_tier)
    obs: dict[bool, dict[str, dict[str, list[int]]]] = {
        False: {ot: {bt: [0, 0] for bt in BOOST_TIERS} for ot in OWNERSHIP_TIERS},
        True:  {ot: {bt: [0, 0] for bt in BOOST_TIERS} for ot in OWNERSHIP_TIERS},
    }
    dates = set()
    for r in rows:
        dates.add(r["date"])
        try:
            drafts = int(float(r["drafts"])) if r["drafts"] else 0
        except ValueError:
            drafts = 0
        try:
            cb = float(r["card_boost"]) if r["card_boost"] else 0.0
        except ValueError:
            cb = 0.0
        is_p = r["position"].strip().upper() in {"P", "SP", "RP"}
        hv = r["is_highest_value"] == "1"
        ot = get_ownership_tier(drafts)
        bt = get_boost_tier(cb)
        obs[is_p][ot][bt][1] += 1
        if hv:
            obs[is_p][ot][bt][0] += 1

    training_dates = sorted(dates)
    print(f"# Trained on {len(rows)} player-appearances across {len(training_dates)} dates")
    print(f"# Dates: {training_dates}\n")

    for label, is_pitcher in [("BATTER", False), ("PITCHER", True)]:
        title = "CONDITION_MATRIX" if not is_pitcher else "PITCHER_CONDITION_MATRIX"
        obs_title = "CONDITION_OBSERVATIONS" if not is_pitcher else "PITCHER_CONDITION_OBSERVATIONS"
        print(f"# ---------- {label} ----------")
        # Print observations
        print(f"{obs_title}: dict[str, dict[str, tuple[int, int]]] = {{")
        for ot in OWNERSHIP_TIERS:
            print(f'    "{ot}": {{')
            for bt in BOOST_TIERS:
                s, t = obs[is_pitcher][ot][bt]
                rate_str = f"{s/t*100:.1f}%" if t > 0 else "no data"
                print(f'        "{bt}":    ({s:3d}, {t:3d}),    # {rate_str}')
            print(f'    }},')
        print(f'}}\n')

        # Print matrix with interpolated + Bayesian smoothed rates
        print(f"{title}: dict[str, dict[str, float]] = {{")
        for ot in OWNERSHIP_TIERS:
            row = {bt: tuple(obs[is_pitcher][ot][bt]) for bt in BOOST_TIERS}
            filled = interpolate_boost_row(row)
            print(f'    "{ot}": {{')
            for bt in BOOST_TIERS:
                s, t = row[bt]
                comment = f"{s}/{t} = {s/t*100:.1f}%" if t > 0 else "interpolated"
                print(f'        "{bt}":    {filled[bt]:.3f},   # {comment}')
            print(f'    }},')
        print(f'}}\n')


if __name__ == "__main__":
    main()
