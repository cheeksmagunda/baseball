from pydantic import BaseModel


class DraftCard(BaseModel):
    player_name: str
    card_boost: float = 0.0


class DraftSlotOut(BaseModel):
    slot_index: int
    slot_mult: float
    player_name: str
    card_boost: float
    estimated_rs: float
    expected_slot_value: float
    player_score: float
    popularity: str = "NEUTRAL"  # FADE, TARGET, or NEUTRAL


class OptimizeRequest(BaseModel):
    cards: list[DraftCard]
    strategy: str = "maximize_ev"  # "maximize_ev" or "maximize_floor"


class OptimizeResponse(BaseModel):
    lineup: list[DraftSlotOut]
    total_expected_value: float
    strategy: str


class EvaluateRequest(BaseModel):
    slots: list[DraftCard]  # 5 cards in slot order


class EvaluateResponse(BaseModel):
    lineup: list[DraftSlotOut]
    total_expected_value: float
    warnings: list[str] = []
