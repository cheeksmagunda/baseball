from pydantic import BaseModel

from app.schemas.scoring import TraitBreakdown


class DraftCard(BaseModel):
    player_name: str
    card_boost: float = 0.0


class DraftSlotOut(BaseModel):
    slot_index: int
    slot_mult: float
    player_name: str
    team: str = ""
    position: str = ""
    card_boost: float
    expected_slot_value: float
    player_score: float
    breakdowns: list[TraitBreakdown] = []


class EvaluateRequest(BaseModel):
    slots: list[DraftCard]  # 5 cards in slot order


class EvaluateResponse(BaseModel):
    lineup: list[DraftSlotOut]
    total_expected_value: float
    warnings: list[str] = []
