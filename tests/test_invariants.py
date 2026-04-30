"""Invariance tests: mechanical defenses for the project's ABSOLUTE rules.

These tests exist to catch regressions that ruff and the static
`audit_live_isolation.py` cannot. Unlike unit tests that verify a specific
output, each test here pins down a *property* the system must always have:

1. Signal isolation — `card_boost`, `drafts`, and `popularity` must NEVER
   appear on FilteredCandidate (CLAUDE.md § "Signal Isolation: ABSOLUTE
   RULE").  Banned at the dataclass-signature level so EV logic is
   structurally incapable of reading them.
2. Schema contract — FilteredCandidate must not declare any of the banned
   historical-outcome OR in-draft dynamic fields.
3. Constant-perturbation rank stability — perturbing each env/trait
   threshold in `app/core/constants.py` by ±10% on a synthetic pool must not
   catastrophically reorder the ranking. Catches brittle cliff-thresholds.
   Note: the fixture is fully synthetic — no historical outcome is read, so
   this is SAFE under the "no automated calibration" rule.
"""

from __future__ import annotations

import dataclasses
import random

import pytest

from app.services.filter_strategy import (
    FilteredCandidate,
    _compute_base_ev,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BANNED_OUTCOME_FIELDS = {
    "real_score",
    "total_value",
    "is_highest_value",
    "is_most_popular",
    "is_most_drafted_3x",
    # In-draft dynamic signals — unknowable pre-game.
    "card_boost",
    "drafts",
    # V11.0: popularity removed from the optimizer entirely.
    "popularity",
    "sharp_score",
}


def _make_candidate(
    *,
    name: str = "Test Player",
    team: str = "NYY",
    is_pitcher: bool = False,
    total_score: float = 50.0,
    env_score: float = 0.6,
    batting_order: int | None = 3,
    env_unknown_count: int = 0,
    game_id: int | str | None = 1,
) -> FilteredCandidate:
    return FilteredCandidate(
        player_name=name,
        team=team,
        position="SP" if is_pitcher else "OF",
        total_score=total_score,
        env_score=env_score,
        env_unknown_count=env_unknown_count,
        game_id=game_id,
        is_pitcher=is_pitcher,
        batting_order=batting_order if not is_pitcher else None,
    )


def _kendall_tau(a: list[str], b: list[str]) -> float:
    """Compute Kendall's tau rank correlation between two orderings of the same items.

    Returns 1.0 for identical ordering, -1.0 for reverse ordering, 0.0 for
    random. Implemented without scipy so the test stays dependency-free.
    """
    if set(a) != set(b):
        raise ValueError("Kendall-tau requires the same item set in both orderings.")
    pos_b = {x: i for i, x in enumerate(b)}
    concordant = 0
    discordant = 0
    n = len(a)
    for i in range(n):
        for j in range(i + 1, n):
            order_a = a[i], a[j]
            order_b_pos = pos_b[order_a[0]], pos_b[order_a[1]]
            if order_b_pos[0] < order_b_pos[1]:
                concordant += 1
            else:
                discordant += 1
    total = concordant + discordant
    return (concordant - discordant) / total if total else 1.0


# ---------------------------------------------------------------------------
# 1. Signal isolation — banned fields must never accept a value
# ---------------------------------------------------------------------------

class TestSignalIsolation:
    """V11.0: card_boost, drafts, popularity, sharp_score do not exist on
    FilteredCandidate at all.  Passing any of these to the dataclass
    constructor raises TypeError, making it structurally impossible for EV
    logic to read them.
    """

    def test_constructor_rejects_card_boost(self):
        with pytest.raises(TypeError):
            FilteredCandidate(
                player_name="x", team="NYY", position="OF",
                total_score=50.0, env_score=0.5, card_boost=1.5,  # type: ignore[call-arg]
            )

    def test_constructor_rejects_drafts(self):
        with pytest.raises(TypeError):
            FilteredCandidate(
                player_name="x", team="NYY", position="OF",
                total_score=50.0, env_score=0.5, drafts=500,  # type: ignore[call-arg]
            )

    def test_constructor_rejects_popularity(self):
        with pytest.raises(TypeError):
            FilteredCandidate(
                player_name="x", team="NYY", position="OF",
                total_score=50.0, env_score=0.5, popularity="FADE",  # type: ignore[call-arg]
            )

    def test_constructor_rejects_sharp_score(self):
        with pytest.raises(TypeError):
            FilteredCandidate(
                player_name="x", team="NYY", position="OF",
                total_score=50.0, env_score=0.5, sharp_score=80.0,  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# 2. Schema contract — FilteredCandidate must not declare banned fields
# ---------------------------------------------------------------------------

class TestSchemaContract:
    """The static audit in scripts/audit_live_isolation.py catches runtime
    attribute reads. This contract test catches the upstream case: someone
    adding a banned field to the FilteredCandidate dataclass itself.
    """

    def test_filtered_candidate_excludes_banned_outcome_fields(self):
        fields = {f.name for f in dataclasses.fields(FilteredCandidate)}
        leaked = fields & BANNED_OUTCOME_FIELDS
        assert not leaked, (
            f"FilteredCandidate declares banned fields: {leaked}. "
            "Historical outcome data, in-draft dynamic signals "
            "(card_boost, drafts), and popularity (V11.0) are never "
            "allowed in the live EV path."
        )


# ---------------------------------------------------------------------------
# 3. Constant-perturbation rank stability
# ---------------------------------------------------------------------------

class TestConstantRankStability:
    """Perturb each constant in the filter_strategy namespace by ±10% on a
    synthetic 20-candidate pool and assert Kendall-tau ≥ 0.78 vs baseline.

    What this catches: a cliff-threshold whose small move causes a large
    fraction of the pool to flip ranks — a sign that the constant is brittle
    and likely overfit to a narrow range of conditions.

    What this does NOT do: calibrate the constants against historical
    outcomes. The pool is synthetic (random env_score, total_score values
    seeded deterministically). No real_score or HV flag is ever read.
    """

    PERTURBATION_TARGETS = [
        # Env modifier bounds — PRIMARY signal
        "ENV_MODIFIER_FLOOR",
        "ENV_MODIFIER_CEILING",
        # Trait modifier bounds — SECONDARY signal
        "TRAIT_MODIFIER_FLOOR",
        "TRAIT_MODIFIER_CEILING",
        # Context multipliers
        "STACK_BONUS",
        "DNP_RISK_PENALTY",
        "DNP_UNKNOWN_PENALTY",
    ]

    TAU_FLOOR = 0.78

    @staticmethod
    def _build_pool(seed: int = 42, size: int = 20) -> list[FilteredCandidate]:
        rng = random.Random(seed)
        pool = []
        for i in range(size):
            pool.append(
                _make_candidate(
                    name=f"P{i}",
                    team=f"T{i % 10}",
                    is_pitcher=(i < size // 5),
                    total_score=rng.uniform(20.0, 90.0),
                    env_score=rng.uniform(0.2, 0.95),
                    batting_order=rng.choice([None, 1, 3, 5, 7, 9]),
                    env_unknown_count=rng.choice([0, 1, 3, 5]),
                )
            )
        return pool

    @staticmethod
    def _rank(pool: list[FilteredCandidate]) -> list[str]:
        # Stable ordering: sort by EV desc, break ties by name for determinism.
        return [
            c.player_name
            for c in sorted(pool, key=lambda c: (-_compute_base_ev(c), c.player_name))
        ]

    @pytest.mark.parametrize("constant_name", PERTURBATION_TARGETS)
    @pytest.mark.parametrize("delta", [-0.10, +0.10])
    def test_rank_is_stable_under_constant_perturbation(
        self, constant_name, delta, monkeypatch
    ):
        from app.services import filter_strategy as fs

        original = getattr(fs, constant_name)
        baseline_pool = self._build_pool()
        baseline_ranking = self._rank(baseline_pool)

        monkeypatch.setattr(fs, constant_name, original * (1.0 + delta))
        perturbed_pool = self._build_pool()  # rebuild with same seed
        perturbed_ranking = self._rank(perturbed_pool)

        tau = _kendall_tau(baseline_ranking, perturbed_ranking)
        assert tau >= self.TAU_FLOOR, (
            f"Ranking collapsed under {delta:+.0%} perturbation of {constant_name}: "
            f"Kendall-tau={tau:.3f} < floor={self.TAU_FLOOR}. "
            "This constant is cliff-like; consider widening its effective range."
        )


# ---------------------------------------------------------------------------
# 4. Kendall-tau helper self-test (keeps the helper honest)
# ---------------------------------------------------------------------------

def test_kendall_tau_self_test():
    assert _kendall_tau(["a", "b", "c"], ["a", "b", "c"]) == 1.0
    assert _kendall_tau(["a", "b", "c"], ["c", "b", "a"]) == -1.0
    # Swapping one adjacent pair out of 3 comparisons → (2 - 1)/3 = 1/3
    assert abs(_kendall_tau(["a", "b", "c"], ["b", "a", "c"]) - (1 / 3)) < 1e-9
