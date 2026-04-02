from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.constants import SUBOPTIMAL_THRESHOLD
from app.core.utils import find_player_by_name
from app.schemas.draft import (
    DraftCard,
    DraftSlotOut,
    OptimizeRequest,
    OptimizeResponse,
    EvaluateRequest,
    EvaluateResponse,
)
from app.services.scoring_engine import score_player
from app.services.draft_optimizer import (
    CardWithScore,
    optimize_lineup,
    evaluate_lineup,
)
from app.services.popularity import PopularityClass, get_popularity_profile

router = APIRouter()


async def _resolve_cards(
    cards: list[DraftCard], db: Session, use_popularity: bool = True
) -> list[CardWithScore]:
    """Look up each card's player, score them, and optionally assess popularity."""
    resolved = []
    for card in cards:
        player = find_player_by_name(db, card.player_name)
        if not player:
            continue

        result = score_player(db, player)

        # Fetch popularity classification if enabled
        pop_class = PopularityClass.NEUTRAL
        if use_popularity:
            try:
                profile = await get_popularity_profile(
                    card.player_name, player.team, result.total_score
                )
                pop_class = profile.classification
            except Exception:
                pass  # Fall back to NEUTRAL if signals fail

        resolved.append(CardWithScore(
            player_name=card.player_name,
            card_boost=card.card_boost,
            score_result=result,
            popularity=pop_class,
        ))
    return resolved


@router.post("/optimize", response_model=OptimizeResponse)
async def optimize_draft(req: OptimizeRequest, db: Session = Depends(get_db)):
    """Given available cards, return the optimal 5-player lineup."""
    if len(req.cards) < 1:
        raise HTTPException(400, "Need at least 1 card")

    cards = await _resolve_cards(req.cards, db)
    if not cards:
        raise HTTPException(404, "No matching players found in database")

    result = optimize_lineup(cards, strategy=req.strategy)

    return OptimizeResponse(
        lineup=[
            DraftSlotOut(
                slot_index=s.slot_index,
                slot_mult=s.slot_mult,
                player_name=s.card.player_name,
                card_boost=s.card.card_boost,
                estimated_rs=s.card.score_result.estimated_rs_mid,
                expected_slot_value=s.expected_slot_value,
                player_score=s.card.score_result.total_score,
                popularity=s.card.popularity.value,
            )
            for s in result.slots
        ],
        total_expected_value=result.total_expected_value,
        strategy=result.strategy,
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
    # Check if this is suboptimal vs optimizer
    optimal = optimize_lineup(cards)
    if optimal.total_expected_value > result.total_expected_value * SUBOPTIMAL_THRESHOLD:
        warnings.append(
            f"Suboptimal slot assignment. Optimal would score "
            f"{optimal.total_expected_value:.1f} vs your {result.total_expected_value:.1f}"
        )

    return EvaluateResponse(
        lineup=[
            DraftSlotOut(
                slot_index=s.slot_index,
                slot_mult=s.slot_mult,
                player_name=s.card.player_name,
                card_boost=s.card.card_boost,
                estimated_rs=s.card.score_result.estimated_rs_mid,
                expected_slot_value=s.expected_slot_value,
                player_score=s.card.score_result.total_score,
                popularity=s.card.popularity.value,
            )
            for s in result.slots
        ],
        total_expected_value=result.total_expected_value,
        warnings=warnings,
    )
