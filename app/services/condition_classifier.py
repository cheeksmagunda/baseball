"""
Condition Classifier — primary ranking signal for the filter pipeline.

Maps each player's (ownership_tier × boost_tier) to their historical HV rate.
This rate, not the trait score, is the first term in the 4-term composite EV formula.

Historical evidence (220 appearances, 11 slates):
  Ghost + Elite Boost (<100 drafts, boost ≥ 2.5): 100% HV rate, avg TV 20.45
  Chalk + Elite Boost (500+ drafts, boost ≥ 2.5):  23% HV rate, avg TV  5.12

The 4× gap is captured directly in the matrix rather than via multiplicative modifiers
that fight the base_ev = total_score × (2 + boost) starting point.
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
# Matrix version & training provenance (Bug 6 — survivorship bias guard)
# ---------------------------------------------------------------------------
# IMPORTANT: Update this version and date list whenever the matrix is retrained.
CONDITION_MATRIX_VERSION = "1.0"
CONDITION_MATRIX_TRAINING_DATES = [
    "2026-03-25", "2026-03-26", "2026-03-27", "2026-03-28", "2026-03-29",
    "2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02", "2026-04-03",
    "2026-04-04", "2026-04-05", "2026-04-06", "2026-04-07", "2026-04-09",
]

# ---------------------------------------------------------------------------
# DEAD_CAPITAL — conditions that should NEVER be drafted.
#
# Four zones where historical data shows 0% edge or negative expectation:
#   HIGH_BOOST + CHALK:       Classic boost trap — 57% bust rate, crowd priced in
#   LOW_BOOST  + CHALK:       Not enough boost to overcome crowd drag
#   LOW_BOOST  + MEGA_CHALK:  Dead money — everyone drafting them
#   NO_BOOST   + MEGA_CHALK:  Dead money — can't generate TV, everyone has them
#
# Returning 0.0 zeros out the entire EV equation and makes it mathematically
# impossible for these players to be drafted.
# ---------------------------------------------------------------------------
DEAD_CAPITAL_CONDITIONS: set[tuple[str, str]] = {
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

def get_ownership_tier(drafts: int | None) -> str:
    """Map draft count to ownership tier. None defaults to 'medium'."""
    if drafts is None:
        return "medium"
    if drafts < 100:
        return "ghost"
    if drafts < 500:
        return "low"
    if drafts < 1000:
        return "medium"
    if drafts < 2000:
        return "chalk"
    return "mega_chalk"


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


def get_condition_hv_rate(drafts: int | None, card_boost: float) -> float:
    """Look up historical HV rate from the condition matrix.

    Returns 0.0 for DEAD_CAPITAL conditions — these are hard-blocked
    so the player can never generate positive EV.
    """
    ownership_tier = get_ownership_tier(drafts)
    boost_tier = get_boost_tier(card_boost)

    if (ownership_tier, boost_tier) in DEAD_CAPITAL_CONDITIONS:
        logger.info(
            "DEAD_CAPITAL hard-block: drafts=%s (tier=%s), boost=%.1f (tier=%s)",
            drafts, ownership_tier, card_boost, boost_tier,
        )
        return 0.0

    return CONDITION_MATRIX[ownership_tier][boost_tier]


def is_auto_include(drafts: int | None, card_boost: float) -> bool:
    """Return True for ghost+elite boost players — the primary historical edge.

    These candidates (< 100 drafts, boost ≥ 2.5) have 82–100% HV rate
    historically and should fill the lineup before lower-tier candidates
    are considered, regardless of trait score.
    """
    if drafts is None:
        return False
    return drafts < AUTO_INCLUDE_DRAFT_THRESHOLD and card_boost >= AUTO_INCLUDE_BOOST_THRESHOLD
