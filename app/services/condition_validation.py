"""
Walk-forward validation for the Condition Matrix.

Detects survivorship bias by training the matrix on a chronological subset
of dates and measuring HV-prediction accuracy on the held-out remainder.

This is a development/calibration tool — it does NOT run in production.
Run it before deploying a new matrix version to confirm the matrix isn't overfit.

Usage (CLI):
    python -m app.services.condition_validation

Usage (import):
    from app.services.condition_validation import walk_forward_validate
    results = walk_forward_validate()
"""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from app.services.condition_classifier import (
    CONDITION_MATRIX_VERSION,
    CONDITION_MATRIX_TRAINING_DATES,
)

# V6.0: get_ownership_tier and get_boost_tier were removed — the V6.0 matrix
# is keyed on (position_type × popularity_class), not (ownership × boost).
# This validation module needs a rewrite for V6.0; stubs prevent import errors.


def get_ownership_tier(drafts, total_slate_drafts=None, slate_draft_distribution=None):
    """V5.0 legacy stub — validation module needs rewrite for V6.0."""
    if drafts is None:
        return "medium"
    if drafts < 100:
        return "ghost"
    if drafts < 200:
        return "low"
    if drafts < 1500:
        return "medium"
    if drafts < 2000:
        return "chalk"
    return "mega_chalk"


def get_boost_tier(card_boost):
    """V5.0 legacy stub — validation module needs rewrite for V6.0."""
    if card_boost < 1.0:
        return "no_boost"
    if card_boost < 2.0:
        return "low_boost"
    if card_boost < 2.5:
        return "mid_boost"
    if card_boost < 3.0:
        return "elite_boost"
    return "max_boost"

logger = logging.getLogger(__name__)

# TV >= 15 is the HV threshold used in the condition matrix
HV_THRESHOLD = 15.0

DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "historical_players.csv"


@dataclass
class TierAccuracy:
    """Accuracy result for a single (ownership_tier, boost_tier) condition."""
    ownership_tier: str
    boost_tier: str
    train_hv_rate: float
    test_hv_rate: float
    test_count: int
    train_count: int
    delta: float = 0.0  # train_hv_rate - test_hv_rate (positive = overfit)


@dataclass
class ValidationResult:
    """Full walk-forward validation result."""
    matrix_version: str
    train_dates: list[str]
    test_dates: list[str]
    tier_results: list[TierAccuracy] = field(default_factory=list)
    overall_train_accuracy: float = 0.0
    overall_test_accuracy: float = 0.0


def _load_historical_data(csv_path: Path | None = None) -> list[dict]:
    """Load historical_players.csv into a list of row dicts."""
    path = csv_path or DATA_PATH
    if not path.exists():
        raise FileNotFoundError(f"Historical data not found: {path}")

    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Parse numeric fields, handling empty strings
            try:
                total_value = float(row["total_value"]) if row.get("total_value") else None
                card_boost = float(row["card_boost"]) if row.get("card_boost") else 0.0
                drafts_raw = row.get("drafts", "")
                drafts = int(float(drafts_raw)) if drafts_raw else None
            except (ValueError, TypeError):
                continue

            if total_value is None:
                continue

            rows.append({
                "date": row["date"],
                "player_name": row.get("player_name", ""),
                "team": row.get("team", ""),
                "total_value": total_value,
                "card_boost": card_boost,
                "drafts": drafts,
            })
    return rows


def _compute_matrix_from_rows(rows: list[dict]) -> dict[str, dict[str, float]]:
    """Compute a condition matrix (HV rates) from a set of player rows."""
    # Accumulate counts: (ownership_tier, boost_tier) → (hv_count, total_count)
    counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])

    for row in rows:
        ownership_tier = get_ownership_tier(row["drafts"])
        boost_tier = get_boost_tier(row["card_boost"])
        key = (ownership_tier, boost_tier)
        counts[key][1] += 1
        if row["total_value"] >= HV_THRESHOLD:
            counts[key][0] += 1

    # Convert to nested dict
    matrix: dict[str, dict[str, float]] = {}
    for (own_tier, bst_tier), (hv, total) in counts.items():
        if own_tier not in matrix:
            matrix[own_tier] = {}
        matrix[own_tier][bst_tier] = hv / total if total > 0 else 0.0

    return matrix


def walk_forward_validate(
    csv_path: Path | None = None,
    train_ratio: float = 0.7,
) -> ValidationResult:
    """
    Walk-forward validation of the condition matrix.

    1. Load historical_players.csv
    2. Split dates chronologically (first train_ratio dates for training)
    3. Compute condition matrix from training dates only
    4. Measure HV prediction accuracy on test dates
    5. Return per-tier and overall accuracy comparison

    Args:
        csv_path: Override path to historical_players.csv
        train_ratio: Fraction of dates to use for training (default 0.7)

    Returns:
        ValidationResult with train vs test accuracy for each tier.
    """
    rows = _load_historical_data(csv_path)
    if not rows:
        raise ValueError("No valid rows in historical data")

    # Get sorted unique dates
    all_dates = sorted(set(r["date"] for r in rows))
    split_idx = max(1, int(len(all_dates) * train_ratio))
    train_dates = all_dates[:split_idx]
    test_dates = all_dates[split_idx:]

    if not test_dates:
        raise ValueError(
            f"Not enough dates for validation. Total dates: {len(all_dates)}, "
            f"train_ratio: {train_ratio}"
        )

    train_date_set = set(train_dates)
    test_date_set = set(test_dates)

    train_rows = [r for r in rows if r["date"] in train_date_set]
    test_rows = [r for r in rows if r["date"] in test_date_set]

    # Compute matrices
    train_matrix = _compute_matrix_from_rows(train_rows)
    test_matrix = _compute_matrix_from_rows(test_rows)

    # Compute per-tier accuracy
    tier_results = []
    all_tiers = set()
    for own_tier in train_matrix:
        for bst_tier in train_matrix[own_tier]:
            all_tiers.add((own_tier, bst_tier))
    for own_tier in test_matrix:
        for bst_tier in test_matrix[own_tier]:
            all_tiers.add((own_tier, bst_tier))

    train_hv_total, train_count_total = 0, 0
    test_hv_total, test_count_total = 0, 0

    # Count occurrences for each tier in train/test
    train_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    test_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])

    for row in train_rows:
        key = (get_ownership_tier(row["drafts"]), get_boost_tier(row["card_boost"]))
        train_counts[key][1] += 1
        if row["total_value"] >= HV_THRESHOLD:
            train_counts[key][0] += 1

    for row in test_rows:
        key = (get_ownership_tier(row["drafts"]), get_boost_tier(row["card_boost"]))
        test_counts[key][1] += 1
        if row["total_value"] >= HV_THRESHOLD:
            test_counts[key][0] += 1

    for own_tier, bst_tier in sorted(all_tiers):
        train_hv, train_n = train_counts.get((own_tier, bst_tier), [0, 0])
        test_hv, test_n = test_counts.get((own_tier, bst_tier), [0, 0])

        train_rate = train_hv / train_n if train_n > 0 else 0.0
        test_rate = test_hv / test_n if test_n > 0 else 0.0

        tier_results.append(TierAccuracy(
            ownership_tier=own_tier,
            boost_tier=bst_tier,
            train_hv_rate=round(train_rate, 3),
            test_hv_rate=round(test_rate, 3),
            test_count=test_n,
            train_count=train_n,
            delta=round(train_rate - test_rate, 3),
        ))

        train_hv_total += train_hv
        train_count_total += train_n
        test_hv_total += test_hv
        test_count_total += test_n

    overall_train = train_hv_total / train_count_total if train_count_total > 0 else 0.0
    overall_test = test_hv_total / test_count_total if test_count_total > 0 else 0.0

    result = ValidationResult(
        matrix_version=CONDITION_MATRIX_VERSION,
        train_dates=train_dates,
        test_dates=test_dates,
        tier_results=tier_results,
        overall_train_accuracy=round(overall_train, 3),
        overall_test_accuracy=round(overall_test, 3),
    )

    logger.info(
        "Walk-forward validation complete: version=%s, train=%d dates, test=%d dates, "
        "train_HV_rate=%.3f, test_HV_rate=%.3f, delta=%.3f",
        result.matrix_version,
        len(train_dates), len(test_dates),
        overall_train, overall_test, overall_train - overall_test,
    )

    return result


def print_validation_report(result: ValidationResult) -> None:
    """Pretty-print a validation result to stdout."""
    print(f"\n{'='*72}")
    print(f"Condition Matrix Walk-Forward Validation (v{result.matrix_version})")
    print(f"{'='*72}")
    print(f"Train dates ({len(result.train_dates)}): {result.train_dates[0]} → {result.train_dates[-1]}")
    print(f"Test dates  ({len(result.test_dates)}):  {result.test_dates[0]} → {result.test_dates[-1]}")
    print(f"\nOverall HV rate: train={result.overall_train_accuracy:.3f}, "
          f"test={result.overall_test_accuracy:.3f}, "
          f"delta={result.overall_train_accuracy - result.overall_test_accuracy:+.3f}")
    print(f"\n{'Ownership':<14} {'Boost':<14} {'Train Rate':>10} {'Test Rate':>10} "
          f"{'Delta':>8} {'Train N':>8} {'Test N':>7}")
    print("-" * 72)

    for t in result.tier_results:
        overfit_flag = " **OVERFIT**" if t.delta > 0.20 and t.test_count >= 5 else ""
        print(f"{t.ownership_tier:<14} {t.boost_tier:<14} {t.train_hv_rate:>10.3f} "
              f"{t.test_hv_rate:>10.3f} {t.delta:>+8.3f} {t.train_count:>8} "
              f"{t.test_count:>7}{overfit_flag}")

    print(f"{'='*72}\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = walk_forward_validate()
    print_validation_report(result)
