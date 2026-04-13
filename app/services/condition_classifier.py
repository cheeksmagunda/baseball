"""
Condition Classifier — primary ranking signal for the filter pipeline.

Maps each player's (ownership_tier × boost_tier) to their historical HV rate.
This rate, not the trait score, is the first term in the 4-term composite EV formula.

Historical evidence (220 appearances, 11 slates):
  Ghost + Elite Boost (<100 drafts, boost ≥ 2.5): 100% HV rate, avg TV 20.45
  Chalk + Elite Boost (500+ drafts, boost ≥ 2.5):  23% HV rate, avg TV  5.12

The 4× gap is captured directly in the matrix rather than via multiplicative modifiers
that fight the base_ev = total_score × (2 + boost) starting point.

V3.0 changes:
  - Replaced DEAD_CAPITAL hard-blocks (0.0) with Bayesian Laplace-smoothed floors.
    A 0/34 observation yields posterior 1/36 ≈ 0.028 under Beta(1,1) prior,
    not a hard 0.0.  This prevents the black-hole effect where zero HV rate
    destroys all upstream/downstream signal, while still heavily discounting
    these conditions.
  - ML model can now contribute signal even for formerly dead-capital conditions.
  - Added CONDITION_OBSERVATIONS matrix tracking (successes, trials) per cell
    for principled Bayesian updating.
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Condition Matrix — historical HV rate by (ownership_tier, boost_tier)
# ---------------------------------------------------------------------------

CONDITION_MATRIX: dict[str, dict[str, float]] = {
    "ghost": {
        "no_boost":    0.35,
        "low_boost":   0.55,
        "mid_boost":   0.75,
        "elite_boost": 0.88,
        "max_boost":   1.00,
    },
    "low": {
        "no_boost":    0.28,
        "low_boost":   0.42,
        "mid_boost":   0.55,
        "elite_boost": 0.62,
        "max_boost":   0.70,
    },
    "medium": {
        "no_boost":    0.22,
        "low_boost":   0.35,
        "mid_boost":   0.38,
        "elite_boost": 0.28,
        "max_boost":   0.23,
    },
    "chalk": {
        "no_boost":    0.18,
        "low_boost":   0.25,
        "mid_boost":   0.28,
        "elite_boost": 0.25,
        "max_boost":   0.23,
    },
    "mega_chalk": {
        "no_boost":    0.15,
        "low_boost":   0.18,
        "mid_boost":   0.20,
        "elite_boost": 0.15,
        "max_boost":   0.12,
    },
}

# ---------------------------------------------------------------------------
# Pitcher-specific condition matrix (V2.5)
#
# SPs structurally get low/no boost from Real Sports because they accumulate
# more game time and stats. An SP with 0 boost is not the same signal as a
# batter with 0 boost — pitchers have a higher RS floor (93% positive RS
# historically) and generate TV through raw RS alone.
#
# Trained on 112 pitcher appearances across 15 dates.
# Key data:  ghost+no_boost=12.5%, low+no_boost=40%, medium+no_boost=0%,
#            ghost+low_boost=100%, ghost+elite_boost=100%.
#            chalk+max_boost=50%, mega_chalk+no_boost=0%.
# ---------------------------------------------------------------------------
PITCHER_CONDITION_MATRIX: dict[str, dict[str, float]] = {
    "ghost": {
        "no_boost":    0.12,   # 1/8 = 12.5% — low but NOT zero
        "low_boost":   1.00,   # 2/2 = 100%
        "mid_boost":   0.75,   # no data, interpolate
        "elite_boost": 1.00,   # 2/2 = 100%
        "max_boost":   1.00,   # 1/1 = 100%
    },
    "low": {
        "no_boost":    0.40,   # 2/5 = 40% — Imanaga's tier, strong
        "low_boost":   1.00,   # 1/1 = 100%
        "mid_boost":   0.60,   # no data, interpolate
        "elite_boost": 0.70,   # no data, interpolate
        "max_boost":   0.50,   # 0/1 small sample, conservative
    },
    "medium": {
        "no_boost":    0.10,   # 0/11 = 0% — round up slightly for small sample
        "low_boost":   0.15,   # 0/1 small sample
        "mid_boost":   0.20,   # no data, interpolate
        "elite_boost": 0.25,   # no data, interpolate
        "max_boost":   0.11,   # 1/9 = 11.1% — Apr 11: Walker (NOT HV), Lopez (NOT HV)
    },
    "chalk": {
        "no_boost":    0.05,   # 0/21 = 0% — round up for small sample
        "low_boost":   0.67,   # 2/3 = 66.7% — boosted chalk SPs can hit
        "mid_boost":   0.40,   # no data, interpolate
        "elite_boost": 0.45,   # no data, interpolate
        "max_boost":   0.42,   # 5/12 = 41.7% — Apr 11: Sheehan (NOT HV), Bassitt (NOT HV)
    },
    "mega_chalk": {
        "no_boost":    0.02,   # 0/35 = 0% — dead money. Apr 11: Fried (NOT HV)
        "low_boost":   0.10,   # no data
        "mid_boost":   0.05,   # 0/2 = 0%
        "elite_boost": 0.10,   # no data
        "max_boost":   0.75,   # 3/4 = 75% — Apr 11: Suarez (HV, TV 28.5)
    },
}

# ---------------------------------------------------------------------------
# Matrix version & training provenance (Bug 6 — survivorship bias guard)
# ---------------------------------------------------------------------------
# IMPORTANT: Update this version and date list whenever the matrix is retrained.
CONDITION_MATRIX_VERSION = "1.1"
CONDITION_MATRIX_TRAINING_DATES = [
    "2026-03-25", "2026-03-26", "2026-03-27", "2026-03-28", "2026-03-29",
    "2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02", "2026-04-03",
    "2026-04-04", "2026-04-05", "2026-04-06", "2026-04-07", "2026-04-09",
    "2026-04-11",
]

# ---------------------------------------------------------------------------
# Bayesian Laplace smoothing (V3.0) — replaces DEAD_CAPITAL hard-blocks.
#
# Instead of returning 0.0 (which destroys all signal), we compute a Bayesian
# posterior using the Beta-Binomial conjugate prior:
#   posterior = (successes + alpha) / (trials + alpha + beta)
#
# With a weak uniform prior (alpha=1, beta=1), a 0/34 observation yields
# 1/36 ≈ 0.028 instead of 0.0.  This allows the ML model and downstream
# pipeline to still operate on these players, while keeping them heavily
# discounted relative to proven conditions.
#
# BAYESIAN_PRIOR_ALPHA and BAYESIAN_PRIOR_BETA control the strength of the
# prior.  alpha=1, beta=1 = uniform (minimally informative).
# ---------------------------------------------------------------------------
BAYESIAN_PRIOR_ALPHA = 1.0
BAYESIAN_PRIOR_BETA = 1.0

# Observation counts: (successes, trials) per (ownership_tier, boost_tier).
# Used to compute Bayesian posterior HV rates.  When the matrix HV rate is
# derived from a known sample, we record it here.  Cells with no data use
# (0, 0) which yields the prior mean (alpha / (alpha + beta) = 0.5), but
# the matrix interpolation value takes precedence for those.
CONDITION_OBSERVATIONS: dict[str, dict[str, tuple[int, int]]] = {
    "ghost": {
        "no_boost":    (5, 14),    # ~35%
        "low_boost":   (6, 11),    # ~55%
        "mid_boost":   (9, 12),    # ~75%
        "elite_boost": (7, 8),     # ~88%
        "max_boost":   (8, 8),     # 100%
    },
    "low": {
        "no_boost":    (7, 25),    # ~28%
        "low_boost":   (5, 12),    # ~42%
        "mid_boost":   (6, 11),    # ~55%
        "elite_boost": (5, 8),     # ~62%
        "max_boost":   (7, 10),    # ~70%
    },
    "medium": {
        "no_boost":    (4, 18),    # ~22%
        "low_boost":   (4, 11),    # ~35%
        "mid_boost":   (3, 8),     # ~38%
        "elite_boost": (2, 7),     # ~28%
        "max_boost":   (2, 9),     # ~23%
    },
    "chalk": {
        "no_boost":    (5, 28),    # ~18%
        "low_boost":   (0, 12),    # 0% observed → Bayesian floor ~0.07
        "mid_boost":   (3, 11),    # ~28%
        "elite_boost": (0, 15),    # 0% observed → Bayesian floor ~0.06
        "max_boost":   (0, 18),    # 0% observed → Bayesian floor ~0.05
    },
    "mega_chalk": {
        "no_boost":    (0, 34),    # 0% observed → Bayesian floor ~0.028
        "low_boost":   (0, 8),     # 0% observed → Bayesian floor ~0.10
        "mid_boost":   (2, 10),    # ~20%
        "elite_boost": (1, 7),     # ~15%
        "max_boost":   (1, 8),     # ~12%
    },
}

PITCHER_CONDITION_OBSERVATIONS: dict[str, dict[str, tuple[int, int]]] = {
    "ghost": {
        "no_boost":    (1, 8),     # 12.5%
        "low_boost":   (2, 2),     # 100%
        "mid_boost":   (0, 0),     # no data
        "elite_boost": (2, 2),     # 100%
        "max_boost":   (1, 1),     # 100%
    },
    "low": {
        "no_boost":    (2, 5),     # 40%
        "low_boost":   (1, 1),     # 100%
        "mid_boost":   (0, 0),     # no data
        "elite_boost": (0, 0),     # no data
        "max_boost":   (0, 1),     # 0%
    },
    "medium": {
        "no_boost":    (0, 11),    # 0%
        "low_boost":   (0, 1),     # 0%
        "mid_boost":   (0, 0),     # no data
        "elite_boost": (0, 0),     # no data
        "max_boost":   (1, 9),     # 11.1% — Apr 11: Walker (NOT HV), Lopez (NOT HV)
    },
    "chalk": {
        "no_boost":    (0, 21),    # 0%
        "low_boost":   (2, 3),     # 66.7%
        "mid_boost":   (0, 0),     # no data
        "elite_boost": (0, 0),     # no data
        "max_boost":   (5, 12),    # 41.7% — Apr 11: Sheehan (NOT HV), Bassitt (NOT HV)
    },
    "mega_chalk": {
        "no_boost":    (0, 35),    # 0% — Apr 11: Fried (NOT HV)
        "low_boost":   (0, 0),     # no data
        "mid_boost":   (0, 2),     # 0%
        "elite_boost": (0, 0),     # no data
        "max_boost":   (3, 4),     # 75% — Apr 11: Suarez (HV, TV 28.5)
    },
}


def bayesian_hv_rate(successes: int, trials: int) -> float:
    """Compute Bayesian posterior mean HV rate using Beta-Binomial conjugate.

    posterior_mean = (successes + alpha) / (trials + alpha + beta)

    With alpha=1, beta=1 (uniform prior):
      0/34 → 1/36 ≈ 0.028  (not 0.0)
      0/8  → 1/10 = 0.10
      8/8  → 9/10 = 0.90   (not 1.0)
    """
    return (successes + BAYESIAN_PRIOR_ALPHA) / (trials + BAYESIAN_PRIOR_ALPHA + BAYESIAN_PRIOR_BETA)


# Legacy constant — retained for reference and logging.  No longer used as a
# hard-block.  These conditions now receive Bayesian floor rates instead of 0.0.
LEGACY_DEAD_CAPITAL_CONDITIONS: set[tuple[str, str]] = {
    ("chalk", "elite_boost"),
    ("chalk", "max_boost"),
    ("chalk", "low_boost"),
    ("mega_chalk", "low_boost"),
    ("mega_chalk", "no_boost"),
}

# ---------------------------------------------------------------------------
# AUTO_INCLUDE threshold — the ghost+boost sweet spot that drives the edge
# ---------------------------------------------------------------------------

AUTO_INCLUDE_DRAFT_THRESHOLD = 100   # < 100 drafts = ghost tier
AUTO_INCLUDE_BOOST_THRESHOLD = 2.5   # boost ≥ 2.5 = elite/max boost tier


# ---------------------------------------------------------------------------
# Tier classification helpers
# ---------------------------------------------------------------------------

def get_ownership_tier(
    drafts: int | None,
    total_slate_drafts: int | None = None,
    slate_draft_distribution: list[int] | None = None,
) -> str:
    """Map draft count to ownership tier.

    V3.0: Primary path uses empirical CDF percentiles from the slate's actual
    draft distribution.  This is slate-size-invariant: a player at the 10th
    percentile of a 2-game slate is treated identically to the 10th percentile
    of a 15-game slate, even if their absolute draft counts differ 10x.

    Tier boundaries (percentile-based):
      ghost:      bottom 15% of the distribution
      low:        15th–35th percentile
      medium:     35th–65th percentile
      chalk:      65th–90th percentile
      mega_chalk: top 10% AND drafts > 3× median (prevents false positives on thin slates)

    Falls back to total-slate-percentage thresholds when the full distribution
    isn't available, and to absolute thresholds as a last resort.
    """
    from app.core.constants import (
        OWNERSHIP_PERCENTILE_GHOST,
        OWNERSHIP_PERCENTILE_LOW,
        OWNERSHIP_PERCENTILE_MEDIUM,
        OWNERSHIP_PERCENTILE_CHALK,
        MEGA_CHALK_MEDIAN_MULTIPLE,
        GHOST_ABSOLUTE_DRAFT_FLOOR,
    )

    if drafts is None:
        return "medium"

    # V3.1: Absolute draft-count floor — micro-drafted players are ALWAYS ghost.
    # DFS draft distributions have extreme right tails.  It's common for 30-40%
    # of the player pool to have exactly 0 drafts.  When this happens, the 15th
    # percentile is mathematically 0, and players with 1-2 drafts (the exact
    # mega-ghosts we're hunting, like Amed Rosario on Apr 7 or Brent Rooker on
    # Apr 5) fall outside the ghost tier.  This floor prevents that.
    if drafts <= GHOST_ABSOLUTE_DRAFT_FLOOR:
        return "ghost"

    # V3.0 Primary: Empirical CDF percentile from actual distribution
    if slate_draft_distribution is not None and len(slate_draft_distribution) >= 5:
        sorted_dist = sorted(slate_draft_distribution)
        n = len(sorted_dist)
        # Compute this player's percentile rank (fraction of players with fewer drafts)
        rank = sum(1 for d in sorted_dist if d < drafts)
        percentile = rank / n

        # Compute median for mega-chalk absolute floor
        median_drafts = sorted_dist[n // 2]

        if percentile < OWNERSHIP_PERCENTILE_GHOST:
            return "ghost"
        if percentile < OWNERSHIP_PERCENTILE_LOW:
            return "low"
        if percentile < OWNERSHIP_PERCENTILE_MEDIUM:
            return "medium"
        if percentile < OWNERSHIP_PERCENTILE_CHALK:
            return "chalk"
        # Top 10%: mega-chalk only if also exceeds absolute floor
        if median_drafts > 0 and drafts > median_drafts * MEGA_CHALK_MEDIAN_MULTIPLE:
            return "mega_chalk"
        return "chalk"  # Top 10% but doesn't meet absolute floor → chalk

    # V2 Secondary: percentage of total slate drafts
    if total_slate_drafts is not None and total_slate_drafts > 0:
        pct = drafts / total_slate_drafts
        if pct < 0.001:
            return "ghost"
        if pct < 0.0035:
            return "low"
        if pct < 0.007:
            return "medium"
        if pct < 0.014:
            return "chalk"
        return "mega_chalk"

    # Absolute fallback (when slate draft totals aren't available)
    from app.core.constants import (
        GHOST_DRAFT_THRESHOLD,
        LOW_DRAFT_THRESHOLD,
        CHALK_DRAFT_THRESHOLD,
        MEGA_CHALK_DRAFT_THRESHOLD,
    )
    if drafts < GHOST_DRAFT_THRESHOLD:   # 100
        return "ghost"
    if drafts < LOW_DRAFT_THRESHOLD:     # 200
        return "low"
    if drafts < CHALK_DRAFT_THRESHOLD:   # 1500
        return "medium"
    if drafts < MEGA_CHALK_DRAFT_THRESHOLD:  # 2000
        return "chalk"
    return "mega_chalk"


def compute_draft_entropy(draft_counts: list[int]) -> float:
    """Compute Shannon entropy of the draft distribution (meta-game monitor).

    V3.0: Higher entropy = more evenly distributed drafts = crowd is getting
    sharper (ghost edge compressing).  Lower entropy = concentrated drafts on
    a few stars = ghost edge intact.

    Track this slate-over-slate to detect meta-game shifts.  A sustained
    increase in entropy over 5+ consecutive slates should trigger a warning
    that the ghost threshold needs recalibration.

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

    V3.0: Gini = 0 means perfectly equal (every player drafted the same
    number of times).  Gini = 1 means maximum inequality (all drafts on one
    player).  The ghost edge thrives on high Gini (crowd concentrates on
    stars, ignoring ghosts).  Falling Gini = ghost edge compression.

    Track alongside entropy for a complete picture.
    """
    if not draft_counts:
        return 0.0
    sorted_counts = sorted(draft_counts)
    n = len(sorted_counts)
    total = sum(sorted_counts)
    if total == 0 or n == 0:
        return 0.0
    cumulative = 0.0
    weighted_sum = 0.0
    for i, count in enumerate(sorted_counts):
        cumulative += count
        weighted_sum += (2 * (i + 1) - n - 1) * count
    return weighted_sum / (n * total)


def get_boost_tier(card_boost: float) -> str:
    """Map card boost value to boost tier."""
    if card_boost < 1.0:
        return "no_boost"
    if card_boost < 2.0:
        return "low_boost"
    if card_boost < 2.5:
        return "mid_boost"
    if card_boost < 3.0:
        return "elite_boost"
    return "max_boost"


def get_condition_hv_rate(
    drafts: int | None,
    card_boost: float,
    is_pitcher: bool = False,
    total_slate_drafts: int | None = None,
) -> float:
    """Look up historical HV rate from the condition matrix, blended with ML.

    V3.0: Replaces DEAD_CAPITAL hard-blocks with Bayesian Laplace-smoothed
    floors.  Formerly dead-capital conditions now receive a small but non-zero
    posterior rate (e.g. 0/34 → 0.028) instead of a hard 0.0.  This prevents
    the black-hole effect where zero HV rate destroys all upstream/downstream
    signal, while still heavily discounting these conditions.

    When observed sample data is available (CONDITION_OBSERVATIONS), the
    Bayesian posterior is used as a floor — the matrix interpolation value
    takes precedence when it exceeds the posterior (e.g. for cells where the
    matrix was conservatively rounded up from small samples).

    When a trained ML model is available, the effective rate is blended with
    the model's P(HV) prediction.  The ML model can now contribute signal
    even for formerly dead-capital conditions.

    When total_slate_drafts is provided, ownership tiers use percentage-based
    thresholds instead of fixed draft counts.
    """
    ownership_tier = get_ownership_tier(drafts, total_slate_drafts)
    boost_tier = get_boost_tier(card_boost)

    is_legacy_dead_capital = False

    if is_pitcher:
        matrix_rate = PITCHER_CONDITION_MATRIX[ownership_tier][boost_tier]
        obs = PITCHER_CONDITION_OBSERVATIONS.get(ownership_tier, {}).get(boost_tier, (0, 0))

        # Pitcher legacy dead capital: mega_chalk + no_boost
        if ownership_tier == "mega_chalk" and boost_tier == "no_boost":
            is_legacy_dead_capital = True
    else:
        matrix_rate = CONDITION_MATRIX[ownership_tier][boost_tier]
        obs = CONDITION_OBSERVATIONS.get(ownership_tier, {}).get(boost_tier, (0, 0))

        if (ownership_tier, boost_tier) in LEGACY_DEAD_CAPITAL_CONDITIONS:
            is_legacy_dead_capital = True

    # Compute effective HV rate from observations or matrix interpolation.
    #
    # V3.5: When observations exist, trust the Bayesian posterior over the
    # hand-interpolated matrix rate.  The old logic used max(matrix, bayesian)
    # which always picked the MORE GENEROUS value — this inflated dead-capital
    # conditions where empirical data showed 0% success but the matrix had an
    # interpolated rate of 20-28% (e.g. chalk+low_boost: 0/12 obs but matrix
    # said 0.25, allowing Yelich-type chalk picks to pass the filter).
    #
    # The Bayesian posterior with Beta(1,1) prior handles small samples
    # gracefully: 0/12 → 0.071, 8/8 → 0.90, 1/1 → 0.667.  For cells with
    # no observations, the matrix interpolation is all we have.
    successes, trials = obs
    if trials > 0:
        effective_rate = bayesian_hv_rate(successes, trials)
    else:
        # No observations — trust the matrix interpolation
        effective_rate = matrix_rate

    if is_legacy_dead_capital:
        logger.info(
            "Bayesian dead-capital rate (V3.5): drafts=%s (tier=%s), boost=%.1f (tier=%s), "
            "obs=%d/%d, bayesian=%.4f, matrix=%.2f, effective=%.4f",
            drafts, ownership_tier, card_boost, boost_tier,
            successes, trials,
            bayesian_hv_rate(successes, trials) if trials > 0 else 0.0,
            matrix_rate, effective_rate,
        )

    # Blend with ML prediction when model is available
    from app.services.ml_model import get_blended_hv_rate
    return get_blended_hv_rate(effective_rate, card_boost, drafts, is_pitcher)


def is_auto_include(
    drafts: int | None,
    card_boost: float,
    total_slate_drafts: int | None = None,
) -> bool:
    """Return True for ghost+elite boost players — the primary historical edge.

    These candidates (ghost tier, boost >= 2.5) have 82-100% HV rate
    historically and should fill the lineup before lower-tier candidates
    are considered, regardless of trait score.

    Uses dynamic ownership thresholds when total_slate_drafts is provided.
    """
    if drafts is None:
        return False
    tier = get_ownership_tier(drafts, total_slate_drafts)
    return tier == "ghost" and card_boost >= AUTO_INCLUDE_BOOST_THRESHOLD


def is_soft_auto_include(
    drafts: int | None,
    card_boost: float,
    total_slate_drafts: int | None = None,
) -> bool:
    """Return True for ghost+mid_boost players — second-tier priority.

    V3.2: Ghost players with boost >= 2.0 (but < 2.5) have a historical
    HV rate of 0.75 — excellent, but below auto-include's 0.88-1.00.
    These candidates get priority over non-ghost players but rank after
    full auto-includes in lineup construction.

    Captures players like James Wood (Apr 10: 52 drafts, 2.0x, TV 16.8)
    who fall below the 2.5 auto-include threshold but still have strong
    condition signals.

    Returns False for players that already qualify as auto_include.
    """
    from app.core.constants import SOFT_AUTO_INCLUDE_BOOST_THRESHOLD

    if drafts is None:
        return False
    # Already auto_include → not soft_auto
    if card_boost >= AUTO_INCLUDE_BOOST_THRESHOLD:
        return False
    tier = get_ownership_tier(drafts, total_slate_drafts)
    return tier == "ghost" and card_boost >= SOFT_AUTO_INCLUDE_BOOST_THRESHOLD
