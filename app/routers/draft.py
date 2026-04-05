import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.database import get_db
from app.core.constants import SUBOPTIMAL_THRESHOLD
from app.core.utils import find_player_by_name
from app.schemas.draft import (
    DraftCard,
    DraftSlotOut,
    LineupOut,
    OptimizeRequest,
    OptimizeResponse,
    DualOptimizeRequest,
    DualOptimizeResponse,
    EvaluateRequest,
    EvaluateResponse,
)
from app.schemas.scoring import TraitBreakdown
from app.services.scoring_engine import score_player
from app.services.draft_optimizer import (
    CardWithScore,
    OptimizedLineup,
    optimize_lineup,
    optimize_dual,
    evaluate_lineup,
)
from app.services.popularity import PopularityClass, get_popularity_profile

router = APIRouter()


def _lineup_to_slots(result: OptimizedLineup) -> list[DraftSlotOut]:
    """Convert an OptimizedLineup to a list of DraftSlotOut for the API."""
    return [
        DraftSlotOut(
            slot_index=s.slot_index,
            slot_mult=s.slot_mult,
            player_name=s.card.player_name,
            team=s.card.score_result.team,
            position=s.card.score_result.position,
            card_boost=s.card.card_boost,
            expected_slot_value=s.expected_slot_value,
            player_score=s.card.score_result.total_score,
            popularity=s.card.popularity.value,
            sharp_score=s.card.sharp_score,
            breakdowns=[
                TraitBreakdown(
                    trait_name=t.name,
                    score=t.score,
                    max_score=t.max_score,
                    raw_value=t.raw_value,
                )
                for t in s.card.score_result.traits
            ],
        )
        for s in result.slots
    ]


async def _resolve_cards(
    cards: list[DraftCard],
    db: Session,
    use_popularity: bool = True,
    include_sharp: bool = False,
) -> list[CardWithScore]:
    """Look up each card's player, score them, and optionally assess popularity."""
    resolved = []
    for card in cards:
        player = find_player_by_name(db, card.player_name)
        if not player:
            continue

        result = score_player(db, player)

        pop_class = PopularityClass.NEUTRAL
        sharp_score = 0.0
        if use_popularity:
            try:
                profile = await get_popularity_profile(
                    card.player_name, player.team, result.total_score,
                    include_sharp=include_sharp,
                )
                pop_class = profile.classification
                sharp_score = profile.sharp_score
            except Exception as exc:
                logger.warning("Popularity fetch failed for %s: %s", card.player_name, exc)

        resolved.append(CardWithScore(
            player_name=card.player_name,
            card_boost=card.card_boost,
            score_result=result,
            popularity=pop_class,
            sharp_score=sharp_score,
        ))
    return resolved


@router.post("/optimize", response_model=OptimizeResponse)
async def optimize_draft(req: OptimizeRequest, db: Session = Depends(get_db)):
    """Given available cards, return the optimal Starting 5 lineup."""
    if len(req.cards) < 1:
        raise HTTPException(400, "Need at least 1 card")

    cards = await _resolve_cards(req.cards, db)
    if not cards:
        raise HTTPException(404, "No matching players found in database")

    result = optimize_lineup(cards, strategy=req.strategy)

    return OptimizeResponse(
        lineup=_lineup_to_slots(result),
        total_expected_value=result.total_expected_value,
        strategy=result.strategy,
    )


@router.post("/dual-optimize", response_model=DualOptimizeResponse)
async def dual_optimize_draft(req: DualOptimizeRequest, db: Session = Depends(get_db)):
    """
    Return both Starting 5 and Moonshot lineups from the same card pool.

    Starting 5: Best EV, standard anti-popularity adjustments.
    Moonshot:   Completely different 5. Heavier TARGET lean, sharp underground
                signal boost, HR power tiebreaker, game diversification.

    Both are competitive to win — Moonshot just swings bigger.
    """
    if len(req.cards) < 6:
        raise HTTPException(400, "Need at least 6 cards for dual lineup (5 + 5 with no overlap)")

    cards = await _resolve_cards(req.cards, db, include_sharp=True)
    if len(cards) < 6:
        raise HTTPException(404, "Not enough matching players found for dual lineup")

    dual = optimize_dual(cards)

    return DualOptimizeResponse(
        starting_5=LineupOut(
            lineup=_lineup_to_slots(dual.starting_5),
            total_expected_value=dual.starting_5.total_expected_value,
            strategy=dual.starting_5.strategy,
        ),
        moonshot=LineupOut(
            lineup=_lineup_to_slots(dual.moonshot),
            total_expected_value=dual.moonshot.total_expected_value,
            strategy=dual.moonshot.strategy,
        ),
    )


@router.post("/evaluate", response_model=EvaluateResponse)
async def evaluate_draft(req: EvaluateRequest, db: Session = Depends(get_db)):
    """Evaluate a user-proposed lineup (cards in slot order)."""
    if len(req.slots) != 5:
        raise HTTPException(400, "Need exactly 5 cards in slot order")

    cards = await _resolve_cards(req.slots, db, use_popularity=False)
    if len(cards) < 5:
        missing = len(req.slots) - len(cards)
        raise HTTPException(404, f"{missing} players not found in database")

    result = evaluate_lineup(cards)

    warnings = []
    optimal = optimize_lineup(cards)
    if optimal.total_expected_value > result.total_expected_value * SUBOPTIMAL_THRESHOLD:
        warnings.append(
            f"Suboptimal slot assignment. Optimal would score "
            f"{optimal.total_expected_value:.1f} vs your {result.total_expected_value:.1f}"
        )

    return EvaluateResponse(
        lineup=_lineup_to_slots(result),
        total_expected_value=result.total_expected_value,
        warnings=warnings,
    )
