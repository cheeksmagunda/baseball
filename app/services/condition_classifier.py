"""
Condition Classifier — meta-game monitoring utilities.

The RS_CONDITION_MATRIX (V6.0–V6.3) was removed in V7.0.  It fed historical
RS ratios into the live EV formula as the primary signal, which violated the
"Filter, Not Forecast" principle — the optimizer was implicitly predicting RS
from past RS outcomes.

Popularity is now a **candidate pool gate only**:
  - FADE players (high pre-game media attention) are excluded from the
    candidate pool before EV computation begins.
  - TARGET / NEUTRAL players are NOT rewarded — they simply pass the gate.
  - The EV formula is driven purely by env (game conditions) and trait
    (season stats), with no RS-derived inputs.

This module retains the meta-game monitoring utilities for observability.
"""

import logging
import math

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Meta-game monitoring
# ---------------------------------------------------------------------------

def compute_draft_entropy(draft_counts: list[int]) -> float:
    """Compute Shannon entropy of the draft distribution (meta-game monitor).

    Higher entropy = more evenly distributed drafts = crowd is getting
    sharper (ghost edge compressing).  Lower entropy = concentrated drafts on
    a few stars = ghost edge intact.

    Returns entropy in bits (log base 2).
    """
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
