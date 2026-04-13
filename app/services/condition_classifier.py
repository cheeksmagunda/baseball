"""
Condition Classifier — primary ranking signal for the filter pipeline.

Maps each player's (ownership_tier × boost_tier) to their historical HV rate.
This rate, not the trait score, is the first term in the 4-term composite EV formula.

V5.0 — 19-Date Retrain (2026-04-13):
  Retrained both matrices from leaderboard-presence appearances (drafts !=
  None) in historical_players.csv across all 19 slates (2026-03-25 →
  2026-04-12), classified using the pipeline's **runtime percentage-path**
  thresholds (pct-of-slate-total drafts) so the matrix buckets match what
  get_ownership_tier produces at inference time.

  Off-leaderboard rows (drafts=None) were excluded from training because
  is_highest_value can only be true for players on a platform leaderboard.
  At runtime, drafts=None now raises in get_ownership_tier — per the
  no-fallback rule, upstream must exclude those candidates explicitly
  rather than substituting a tier.

  Batter corrections vs V4.0 (selected):
    ghost+no_boost:    0.611 → 0.917  (10/10 — unboosted leaderboard ghosts
                                        all HV)
    ghost+elite_boost: 0.500 → 0.962  (24/24 — V4.0 was badly wrong)
    ghost+max_boost:   0.793 → 0.972  (137/140)
    mega_chalk+max:    0.200 → 0.136  (5/42 — boost on high-ownership batters
                                        is a trap)

  Pitcher corrections vs V4.0 (selected):
    mega_chalk+no_boost:  0.194 → 0.183  (12/69 — Fried/Alcantara/Sale
                                          class, decent unboosted anchor)
    mega_chalk+max_boost: 0.600 → 0.455  (14/31 — Suarez/Cole class. The
                                          Apr 11 Sheehan/Bassitt busts
                                          pulled this down from V4.0's
                                          0.60, but it's still the single
                                          safest anchor cell when available)
    Lower-ownership pitcher cells collapsed to priors (tiny samples under
    the percentage path — most SPs end up in mega_chalk).

  Bayesian Laplace smoothing with Beta(1,1) prior is applied to every observed
  cell: rate = (successes + 1) / (trials + 2).  This protects against 0/N
  outliers (prevents hard zeros) and pulls tiny samples toward the prior mean.
  Cells with no observations are linearly interpolated from neighbors on the
  same ownership row.

V3.0 changes (retained):
  - Bayesian Laplace-smoothed floors replace DEAD_CAPITAL hard-blocks (0.0).
  - ML model can contribute signal even for formerly dead-capital conditions.
  - CONDITION_OBSERVATIONS tracks (successes, trials) per cell for updating.
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Condition Matrix — historical HV rate by (ownership_tier, boost_tier)
# ---------------------------------------------------------------------------

CONDITION_MATRIX: dict[str, dict[str, float]] = {
    # Bayesian posterior means (Beta(1,1) prior) from 19 slates (2026-03-25 →
    # 2026-04-12), leaderboard-presence rows only, classified via the pipeline's
    # runtime PERCENTAGE path (pct of slate-total drafts): ghost<0.1%, low<0.35%,
    # medium<0.7%, chalk<1.4%, mega_chalk≥1.4%.  Training matches what
    # get_ownership_tier actually produces at inference time.
    "ghost": {
        "no_boost":    0.917,  # 10/10  = 100.0%
        "low_boost":   0.900,  #  8/8   = 100.0%
        "mid_boost":   0.923,  # 11/11  = 100.0%
        "elite_boost": 0.962,  # 24/24  = 100.0%
        "max_boost":   0.972,  # 137/140 = 97.9%
    },
    "low": {
        "no_boost":    0.667,  #  1/1   = 100.0% (small sample — smoothed)
        "low_boost":   0.875,  #  6/6   = 100.0%
        "mid_boost":   0.857,  #  5/5   = 100.0%
        "elite_boost": 0.833,  #  4/4   = 100.0%
        "max_boost":   0.667,  #  1/1   = 100.0% (small sample — smoothed)
    },
    "medium": {
        "no_boost":    0.800,  #  3/3   = 100.0%
        "low_boost":   0.667,  #  1/1   = 100.0%
        "mid_boost":   0.875,  #  6/6   = 100.0%
        "elite_boost": 0.667,  #  1/1   = 100.0%
        "max_boost":   0.333,  #  0/1   = 0.0%   (tiny sample, smoothed)
    },
    "chalk": {
        "no_boost":    0.800,  #  3/3   = 100.0% (small sample — smoothed)
        "low_boost":   0.667,  #  1/1   = 100.0%
        "mid_boost":   0.505,  # interpolated
        "elite_boost": 0.343,  # interpolated
        "max_boost":   0.182,  #  1/9   = 11.1%
    },
    "mega_chalk": {
        "no_boost":    0.136,  #  2/20  = 10.0%
        "low_boost":   0.042,  #  4/118 = 3.4%   (dead capital, n=118)
        "mid_boost":   0.158,  #  2/17  = 11.8%
        "elite_boost": 0.429,  #  2/5   = 40.0%  (small sample — smoothed)
        "max_boost":   0.136,  #  5/42  = 11.9%  (boost trap on high-ownership)
    },
}

# ---------------------------------------------------------------------------
# Pitcher-specific condition matrix
#
# SPs structurally get low/no boost from Real Sports because they accumulate
# more game time and stats. An SP with 0 boost is not the same signal as a
# batter with 0 boost — pitchers have a higher RS floor and generate TV
# through raw RS alone.  Elite aces (Sale, Alcantara, Fried, Gausman, Skubal)
# frequently HV with 0 boost against weak offenses — the matrix must reflect
# this or those anchor plays get buried by the unboosted-pitcher penalty.
#
# V4.0: Retrained on 114 pitcher appearances across 16 dates (2026-03-25
# through 2026-04-10) from historical_players.csv.  Bayesian smoothed with
# Beta(1,1) prior.
# ---------------------------------------------------------------------------
PITCHER_CONDITION_MATRIX: dict[str, dict[str, float]] = {
    # V5.0 retrain: 19 slates, percentage-path tier classification.
    # Under the percentage path, virtually every named SP ends up in "mega_chalk"
    # because pitchers draw high concentrated draft counts relative to slate
    # totals — the "chalk"/"medium" pitcher buckets barely exist empirically.
    "ghost": {
        "no_boost":    0.889,  #  7/7   = 100.0%
        "low_boost":   0.667,  #  1/1   = 100.0%
        "mid_boost":   0.708,  # interpolated
        "elite_boost": 0.750,  #  2/2   = 100.0%
        "max_boost":   0.667,  #  1/1   = 100.0% (small sample — smoothed)
    },
    "low": {
        "no_boost":    0.833,  #  4/4   = 100.0%
        "low_boost":   0.667,  #  1/1   = 100.0%
        "mid_boost":   0.667,  # interpolated
        "elite_boost": 0.667,  # interpolated
        "max_boost":   0.667,  #  1/1   = 100.0%
    },
    "medium": {
        "no_boost":    0.750,  #  2/2   = 100.0%
        "low_boost":   0.667,  #  1/1   = 100.0%
        "mid_boost":   0.667,  # interpolated
        "elite_boost": 0.667,  # interpolated
        "max_boost":   0.667,  # interpolated
    },
    "chalk": {
        "no_boost":    0.750,  #  2/2   = 100.0% (small sample — smoothed)
        "low_boost":   0.667,  #  1/1   = 100.0%
        "mid_boost":   0.611,  # interpolated
        "elite_boost": 0.556,  # interpolated
        "max_boost":   0.500,  #  1/2   = 50.0% (small sample — smoothed)
    },
    "mega_chalk": {
        "no_boost":    0.183,  # 12/69  = 17.4% (Fried/Alcantara/Sale/Skubal —
                               #                 unboosted aces, decent anchor)
        "low_boost":   0.250,  #  1/6   = 16.7%
        "mid_boost":   0.500,  #  1/2   = 50.0% (small sample — smoothed)
        "elite_boost": 0.477,  # interpolated
        "max_boost":   0.455,  # 14/31  = 45.2% (Suarez/Cole-class top chalk SP
                               #                 with heavy boost — the strongest
                               #                 anchor when available)
    },
}

# ---------------------------------------------------------------------------
# Matrix version & training provenance (Bug 6 — survivorship bias guard)
# ---------------------------------------------------------------------------
# IMPORTANT: Update this version and date list whenever the matrix is retrained.
# Recalibrate with: python3 scripts/recalibrate_condition_matrix.py
CONDITION_MATRIX_VERSION = "5.0"
CONDITION_MATRIX_TRAINING_DATES = [
    "2026-03-25", "2026-03-26", "2026-03-27", "2026-03-28", "2026-03-29",
    "2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02", "2026-04-03",
    "2026-04-04", "2026-04-05", "2026-04-06", "2026-04-07", "2026-04-08",
    "2026-04-09", "2026-04-10", "2026-04-11", "2026-04-12",
]

# ---------------------------------------------------------------------------
# Bayesian Laplace smoothing — replaces the legacy DEAD_CAPITAL hard-blocks.
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
    # V5.0: Retrained from historical_players.csv, 19 slates 2026-03-25 →
    # 2026-04-12, via runtime percentage-path tier classification.
    "ghost": {
        "no_boost":    ( 10,  10),  # 100.0%
        "low_boost":   (  8,   8),  # 100.0%
        "mid_boost":   ( 11,  11),  # 100.0%
        "elite_boost": ( 24,  24),  # 100.0%
        "max_boost":   (137, 140),  # 97.9%
    },
    "low": {
        "no_boost":    (  1,   1),  # 100.0%
        "low_boost":   (  6,   6),  # 100.0%
        "mid_boost":   (  5,   5),  # 100.0%
        "elite_boost": (  4,   4),  # 100.0%
        "max_boost":   (  1,   1),  # 100.0%
    },
    "medium": {
        "no_boost":    (  3,   3),  # 100.0%
        "low_boost":   (  1,   1),  # 100.0%
        "mid_boost":   (  6,   6),  # 100.0%
        "elite_boost": (  1,   1),  # 100.0%
        "max_boost":   (  0,   1),  # 0.0% (tiny sample)
    },
    "chalk": {
        "no_boost":    (  3,   3),  # 100.0%
        "low_boost":   (  1,   1),  # 100.0%
        "mid_boost":   (  0,   0),  # no data → interpolated
        "elite_boost": (  0,   0),  # no data → interpolated
        "max_boost":   (  1,   9),  # 11.1%
    },
    "mega_chalk": {
        "no_boost":    (  2,  20),  # 10.0%
        "low_boost":   (  4, 118),  # 3.4% (dead capital, n=118)
        "mid_boost":   (  2,  17),  # 11.8%
        "elite_boost": (  2,   5),  # 40.0%
        "max_boost":   (  5,  42),  # 11.9% (boost trap on high-ownership batters)
    },
}

PITCHER_CONDITION_OBSERVATIONS: dict[str, dict[str, tuple[int, int]]] = {
    # V5.0: Retrained from historical_players.csv, 19 slates, percentage-path.
    "ghost": {
        "no_boost":    (  7,   7),  # 100.0%
        "low_boost":   (  1,   1),  # 100.0%
        "mid_boost":   (  0,   0),  # no data → interpolated
        "elite_boost": (  2,   2),  # 100.0%
        "max_boost":   (  1,   1),  # 100.0%
    },
    "low": {
        "no_boost":    (  4,   4),  # 100.0%
        "low_boost":   (  1,   1),  # 100.0%
        "mid_boost":   (  0,   0),  # no data → interpolated
        "elite_boost": (  0,   0),  # no data → interpolated
        "max_boost":   (  1,   1),  # 100.0%
    },
    "medium": {
        "no_boost":    (  2,   2),  # 100.0%
        "low_boost":   (  1,   1),  # 100.0%
        "mid_boost":   (  0,   0),  # no data → interpolated
        "elite_boost": (  0,   0),  # no data → interpolated
        "max_boost":   (  0,   0),  # no data → interpolated
    },
    "chalk": {
        "no_boost":    (  2,   2),  # 100.0%
        "low_boost":   (  1,   1),  # 100.0%
        "mid_boost":   (  0,   0),  # no data → interpolated
        "elite_boost": (  0,   0),  # no data → interpolated
        "max_boost":   (  1,   2),  # 50.0%
    },
    "mega_chalk": {
        "no_boost":    ( 12,  69),  # 17.4% — Fried/Alcantara/Sale/Skubal
        "low_boost":   (  1,   6),  # 16.7%
        "mid_boost":   (  1,   2),  # 50.0%
        "elite_boost": (  0,   0),  # no data → interpolated
        "max_boost":   ( 14,  31),  # 45.2% — Suarez/Cole class (top chalk SP
                                    #          + heavy boost = strongest anchor
                                    #          when available)
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
#
# V4.0: chalk+elite_boost removed (1/2 observed → 0.50 Bayesian, not dead).
# mega_chalk+no_boost retained as "legacy" for logging purposes even though
# empirical rate is 0.194 — elite aces without boost still have variance worth
# tracking.
LEGACY_DEAD_CAPITAL_CONDITIONS: set[tuple[str, str]] = {
    ("chalk", "max_boost"),      # 0/8 observed → Bayesian 0.10
    ("chalk", "low_boost"),      # 0/30 observed → Bayesian 0.031
    ("mega_chalk", "low_boost"), # 1/21 observed → Bayesian 0.087
    ("mega_chalk", "no_boost"),  # 1/11 observed → Bayesian 0.154
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

    Primary path uses empirical CDF percentiles from the slate's actual
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
        # No-fallback rule: off-leaderboard players should never reach this
        # classifier — they have no draft count, so we can't classify them.
        # Upstream must filter drafts=None out of the candidate pool rather
        # than substituting a tier here.
        raise ValueError(
            "get_ownership_tier called with drafts=None — off-leaderboard "
            "players must be excluded from the candidate pool upstream; "
            "no fallback tier will be substituted."
        )

    # Absolute draft-count floor — micro-drafted players are ALWAYS ghost.
    # DFS draft distributions have extreme right tails.  It's common for 30-40%
    # of the player pool to have exactly 0 drafts.  When this happens, the 15th
    # percentile is mathematically 0, and players with 1-2 drafts (the exact
    # mega-ghosts we're hunting, like Amed Rosario on Apr 7 or Brent Rooker on
    # Apr 5) fall outside the ghost tier.  This floor prevents that.
    if drafts <= GHOST_ABSOLUTE_DRAFT_FLOOR:
        return "ghost"

    # Primary: Empirical CDF percentile from actual distribution
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

    # Secondary: percentage of total slate drafts
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

    Higher entropy = more evenly distributed drafts = crowd is getting
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

    Gini = 0 means perfectly equal (every player drafted the same
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

    Replaces DEAD_CAPITAL hard-blocks with Bayesian Laplace-smoothed
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
    # When observations exist, trust the Bayesian posterior over the
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
            "Bayesian dead-capital rate: drafts=%s (tier=%s), boost=%.1f (tier=%s), "
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
    """Return True for the primary historical edge cells.

    Two auto-include paths (either qualifies):

    1. Classic ghost+elite-boost: ghost tier AND boost >= 2.5.
       Retrained V5.0 matrix shows 96-97% HV rate for ghost+elite/max_boost
       (n=24 and n=140 across 19 slates).

    2. Mega-ghost any-boost (drafts <= GHOST_ABSOLUTE_DRAFT_FLOOR = 25):
       Retrained matrix shows 100% HV rate for the ghost row at every boost
       tier (no_boost 10/10, low 8/8, mid 11/11, elite 24/24, max 137/140).
       The boost gate was an artefact of small early samples — mega-ghosts
       win the HV leaderboard at a near-ceiling rate regardless of boost.
       Widening to unboosted mega-ghosts catches plays the V4.x optimizer
       was systematically skipping.

    Uses dynamic ownership thresholds when total_slate_drafts is provided.
    """
    from app.core.constants import GHOST_ABSOLUTE_DRAFT_FLOOR

    if drafts is None:
        return False
    tier = get_ownership_tier(drafts, total_slate_drafts)
    if tier == "ghost" and card_boost >= AUTO_INCLUDE_BOOST_THRESHOLD:
        return True
    # Path 2: mega-ghost (drafts <= 25), all boost tiers.
    if drafts <= GHOST_ABSOLUTE_DRAFT_FLOOR:
        return True
    return False


def is_soft_auto_include(
    drafts: int | None,
    card_boost: float,
    total_slate_drafts: int | None = None,
) -> bool:
    """Return True for ghost+mid_boost players — second-tier priority.

    Ghost players with boost >= 2.0 (but < 2.5) have a historical
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
