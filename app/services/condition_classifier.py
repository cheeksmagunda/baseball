"""
Condition Classifier — V6.0 "Popularity-First Side Analysis"

Maps each player's (position_type × popularity_class) to their historical
RS performance ratio.  This ratio, not the trait score, is the dominant
term in the EV formula.

V6.0 — Popularity-First Rewrite (2026-04-14):
  The V5.0 matrix was keyed on (ownership_tier × boost_tier) — both of which
  come from the Real Sports platform and are unknowable pre-game.  This made
  the matrix useless for live slates: every player defaulted to "medium" tier
  with 0.0 boost, collapsing the entire strategy.

  V6.0 rekeys the matrix on signals we CAN observe before the slate:
    - popularity_class: FADE / TARGET / NEUTRAL — from web-scraped external
      signals (Google Trends, ESPN RSS, Reddit buzz).
      NOTE: DFS platform ownership (RotoGrinders, NumberFire) is intentionally
      EXCLUDED — those numbers are only visible during the draft session, not
      before it.  See app/services/popularity.py for the actual signal sources.
    - position_type: pitcher vs batter — structural RS distribution difference

  Empirical evidence (20 dates, 2026-03-25 → 2026-04-13):
    Batter+TARGET:  avg RS 3.57, HV rate 73.6% (n=311)
    Batter+FADE:    avg RS 0.98, HV rate  9.6% (n=177)
    Batter+FADE_3X: avg RS 0.79, HV rate  8.9% (n=56)
    Pitcher+TARGET: avg RS 4.36, HV rate 44.7% (n=47)
    Pitcher+FADE:   avg RS 3.09, HV rate 19.3% (n=83)

  The popularity signal produces a 3.6x RS differential for batters
  (TARGET 3.57 vs FADE 0.98).  The crowd is structurally wrong about
  batters but less wrong about pitchers (1.4x differential).

  This matrix replaces both CONDITION_MATRIX and PITCHER_CONDITION_MATRIX.
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# V6.0 RS Condition Matrix — (position_type × popularity_class) → RS factor
#
# Values are empirical RS ratios relative to TARGET baseline (= 1.00),
# computed from 20 dates of historical_players.csv.
#
# The RS factor directly multiplies into the EV formula as the primary
# signal.  A batter classified FADE starts at 0.27x — meaning they need
# 3.7x better env + trait scores to overcome a TARGET batter.  This is
# calibrated to the actual historical RS differential.
# ---------------------------------------------------------------------------

RS_CONDITION_MATRIX: dict[str, dict[str, float]] = {
    # Batters: crowd-avoidance is the dominant edge.
    # TARGET batters average RS 3.57, FADE batters average 0.98 (3.6x ratio).
    # Non-popular players hit HV at 73.6% vs 9.6% for popular ones.
    "batter": {
        "TARGET":  1.000,   # n=311, avg RS 3.57, HV 73.6% — baseline
        "NEUTRAL": 0.650,   # interpolated — moderate buzz, partial crowd info
        "FADE":    0.275,   # n=177, avg RS 0.98, HV 9.6%  — crowd is here
    },
    # Pitchers: crowd is structurally less wrong (one-player performance).
    # TARGET pitchers avg RS 4.36, FADE pitchers avg 3.09 (1.4x ratio).
    # Pitchers control their own environment — high attention often reflects
    # real ERA/K-rate quality, not just media hype.
    "pitcher": {
        "TARGET":  1.000,   # n=47, avg RS 4.36, HV 44.7% — baseline
        "NEUTRAL": 0.850,   # interpolated — moderate pitcher buzz
        "FADE":    0.710,   # n=83, avg RS 3.09, HV 19.3% — crowd less wrong
    },
}

# Observation counts for Bayesian updating.  (successes, trials) where
# "success" = player had RS > 3.0 (a proxy for positive DFS contribution).
RS_CONDITION_OBSERVATIONS: dict[str, dict[str, tuple[int, int]]] = {
    # Updated with April 14 data (V6.1):
    # Apr 14 TARGET batters drafted: Devers (RS -0.3), Henderson (RS -0.7),
    #   Yoshida (RS 0.6), Rosario (RS 0.1) — all < 3.0 → 0 successes, 4 trials
    # Apr 14 TARGET batter not drafted: Buxton (RS > 3.0) → +1 success, +1 trial
    # Net batter TARGET: +1 success, +5 trials (4 busts + 1 winner)
    "batter": {
        "TARGET":  (201, 316),   # 63.6% RS > 3.0  (was 200/311; +1 success Buxton, +5 trials)
        "NEUTRAL": (  0,   0),   # no direct observations — interpolated
        "FADE":    ( 18, 177),   # 10.2% RS > 3.0 (no new Apr 14 FADE batter data)
    },
    # Apr 14 FADE pitcher: Gore RS 2.0 (< 3.0) → +0 successes, +1 trial
    "pitcher": {
        "TARGET":  ( 34,  47),   # 72.3% RS > 3.0 (no new Apr 14 TARGET pitcher data)
        "NEUTRAL": (  0,   0),   # no direct observations — interpolated
        "FADE":    ( 48,  84),   # 57.1% RS > 3.0  (was 48/83; +0 successes, +1 trial Gore)
    },
}

# ---------------------------------------------------------------------------
# Matrix version & training provenance
# ---------------------------------------------------------------------------
# V6.1 — April 14 post-mortem update:
#   Added April 14 observations.  All four drafted TARGET-ish batters busted:
#   Devers RS -0.3 (STL), Henderson RS -0.7 (BAL), Yoshida RS 0.6 (BOS,
#   vs dominant Twins), Rosario RS 0.1 (MIL).  Gore RS 2.0 (FADE pitcher,
#   641 drafts).  Buxton (MIN) was highest value (TARGET batter, big game).
#   Net effect: TARGET batter success count +1 (Buxton), FADE batter + 0,
#   observation counts updated below.  The April 14 bust was an env-signal
#   failure (missing Vegas/bullpen/series data), not a matrix error.
CONDITION_MATRIX_VERSION = "6.1"
CONDITION_MATRIX_TRAINING_DATES = [
    "2026-03-25", "2026-03-26", "2026-03-27", "2026-03-28", "2026-03-29",
    "2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02", "2026-04-03",
    "2026-04-04", "2026-04-05", "2026-04-06", "2026-04-07", "2026-04-08",
    "2026-04-09", "2026-04-10", "2026-04-11", "2026-04-12", "2026-04-13",
    "2026-04-14",
]

# ---------------------------------------------------------------------------
# Bayesian smoothing (retained from V3.0+)
# ---------------------------------------------------------------------------
BAYESIAN_PRIOR_ALPHA = 1.0
BAYESIAN_PRIOR_BETA = 1.0


def bayesian_rate(successes: int, trials: int) -> float:
    """Compute Bayesian posterior mean using Beta-Binomial conjugate.

    posterior_mean = (successes + alpha) / (trials + alpha + beta)

    With alpha=1, beta=1 (uniform prior):
      0/34 → 1/36 ≈ 0.028  (not 0.0)
      8/8  → 9/10 = 0.90   (not 1.0)
    """
    return (successes + BAYESIAN_PRIOR_ALPHA) / (trials + BAYESIAN_PRIOR_ALPHA + BAYESIAN_PRIOR_BETA)


# ---------------------------------------------------------------------------
# Core lookup: (position_type, popularity_class) → RS condition factor
# ---------------------------------------------------------------------------

def get_rs_condition_factor(
    popularity_class: str,
    is_pitcher: bool = False,
) -> float:
    """Look up the RS condition factor from the V6.0 matrix.

    This is the primary term in the EV formula.  A TARGET batter gets 1.00,
    a FADE batter gets 0.275 — the crowd-avoidance signal is the strongest
    predictor of RS across 20 dates of data.

    Args:
        popularity_class: "FADE", "NEUTRAL", or "TARGET" from web scraping
        is_pitcher: True for starting pitchers, False for batters

    Returns:
        RS condition factor (0.275 – 1.00 for batters, 0.71 – 1.00 for pitchers)
    """
    pos_key = "pitcher" if is_pitcher else "batter"
    matrix_row = RS_CONDITION_MATRIX[pos_key]

    # Normalize classification to matrix keys
    pop_key = popularity_class.upper() if popularity_class else "NEUTRAL"
    if pop_key not in matrix_row:
        pop_key = "NEUTRAL"

    factor = matrix_row[pop_key]

    # Blend with Bayesian posterior when observations exist
    obs = RS_CONDITION_OBSERVATIONS.get(pos_key, {}).get(pop_key, (0, 0))
    successes, trials = obs
    if trials > 0:
        bayesian = bayesian_rate(successes, trials)
        # The matrix factor and Bayesian rate measure different things
        # (RS ratio vs P(RS>3)), but both express the same directional signal.
        # Use the matrix factor as the primary value — it's calibrated for
        # the EV formula.  Log Bayesian for monitoring.
        logger.debug(
            "RS condition: %s+%s → factor=%.3f, bayesian_p(RS>3)=%.3f "
            "(obs=%d/%d)",
            pos_key, pop_key, factor, bayesian, successes, trials,
        )

    return factor


# ---------------------------------------------------------------------------
# Meta-game monitoring (retained from V3.0)
# ---------------------------------------------------------------------------

def compute_draft_entropy(draft_counts: list[int]) -> float:
    """Compute Shannon entropy of the draft distribution (meta-game monitor).

    Higher entropy = more evenly distributed drafts = crowd is getting
    sharper (ghost edge compressing).  Lower entropy = concentrated drafts on
    a few stars = ghost edge intact.

    Returns entropy in bits (log base 2).
    """
    import math
    if not draft_counts:
        return 0.0
    total = sum(draft_counts)
    if total == 0:
        return 0.0
    entropy = 0.0
    for count in draft_counts:
        if count > 0:
            p = count / total
            entropy -= p * math.log2(p)
    return entropy


def compute_gini_coefficient(draft_counts: list[int]) -> float:
    """Compute Gini coefficient of the draft distribution (meta-game monitor).

    Gini = 0 means perfectly equal.  Gini = 1 means maximum inequality.
    The ghost edge thrives on high Gini.  Falling Gini = ghost edge compression.
    """
    if not draft_counts:
        return 0.0
    sorted_counts = sorted(draft_counts)
    n = len(sorted_counts)
    total = sum(sorted_counts)
    if total == 0 or n == 0:
        return 0.0
    weighted_sum = 0.0
    for i, count in enumerate(sorted_counts):
        weighted_sum += (2 * (i + 1) - n - 1) * count
    return weighted_sum / (n * total)
