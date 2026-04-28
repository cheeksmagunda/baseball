from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.constants import SUBOPTIMAL_THRESHOLD
from app.core.utils import find_player_by_name
from app.schemas.draft import (
    DraftCard,
    DraftSlotOut,
    EvaluateRequest,
    EvaluateResponse,
)
from app.schemas.scoring import TraitBreakdown
from app.services.scoring_engine import score_player
from app.services.draft_optimizer import (
    CardWithScore,
    OptimizedLineup,
    optimize_lineup,
    evaluate_lineup,
)

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


def _resolve_cards(
    cards: list[DraftCard], db: Session
) -> tuple[list[CardWithScore], list[str]]:
    """Look up each card's player and score them.

    Returns (resolved_cards, missing_names) so the caller can surface which
    specific players were not found — far more actionable than a generic count.
    """
    resolved = []
    missing = []
    for card in cards:
        player = find_player_by_name(db, card.player_name)
        if not player:
            missing.append(card.player_name)
            continue
        result = score_player(db, player)
        resolved.append(CardWithScore(
            player_name=card.player_name,
            card_boost=card.card_boost,
            score_result=result,
        ))
    return resolved, missing


@router.post("/evaluate", response_model=EvaluateResponse)
def evaluate_draft(req: EvaluateRequest, db: Session = Depends(get_db)):
    """Evaluate a user-proposed lineup (cards in slot order)."""
    if len(req.slots) != 5:
        raise HTTPException(400, "Need exactly 5 cards in slot order")

    cards, missing = _resolve_cards(req.slots, db)
    if missing:
        raise HTTPException(
            404,
            f"Players not found in database: {', '.join(missing)}"
        )

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
