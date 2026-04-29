"""
Filter Strategy V10.4: "Filter, Not Forecast" + conditional stacking.

Public API (imported by pipeline.py, routers/filter_strategy.py, and tests)
────────────────────────────────────────────────────────────────────────────
Slate classification:
  SlateType, SlateClassification, StackableGame
  classify_slate(game_count, games) -> SlateClassification

Environmental scoring:
  compute_pitcher_env_score(...) -> tuple[float, dict]
  compute_batter_env_score(...)  -> tuple[float, dict, int]

Candidate model:
  FilteredCandidate   — input to all optimization functions
  FilterSlotAssignment, FilterOptimizedLineup — output structures

Dual-lineup optimizer:
  run_filter_strategy(candidates, slate_class)      -> FilterOptimizedLineup
  run_dual_filter_strategy(candidates, slate_class) -> DualFilterOptimizedResult

Internal helpers (used by tests and pipeline but not by external callers):
  _compute_base_ev, _compute_filter_ev, _compute_moonshot_filter_ev
  _exclude_fade_players, _enforce_composition, _validate_lineup_structure
  _smart_slot_assignment, _compute_dnp_adjustment, _compute_stack_eligible_teams
────────────────────────────────────────────────────────────────────────────

Module sections (by line range, approximate):
  1. Slate classification   — SlateType / SlateClassification / classify_slate
  2. Env scoring helpers    — graduated_scale helpers shared with utils
  3. Pitcher env scoring    — compute_pitcher_env_score
  4. Batter env scoring     — compute_batter_env_score (Groups A/B/C/D)
  5. Candidate data model   — FilteredCandidate, FilterSlotAssignment, etc.
  6. EV computation         — _compute_base_ev, _compute_filter_ev, moonshot
  7. Composition engine     — _enforce_composition, _validate_lineup_structure
  8. Slot assignment        — _smart_slot_assignment
  9. Public optimizers      — run_filter_strategy, run_dual_filter_strategy

────────────────────────────────────────────────────────────────────────────

This is the core strategic engine from the Master Strategy Document.
We do NOT predict RS. We identify conditions under which high RS is
most likely to emerge, then select from that filtered pool.

Five filters applied sequentially:
  1. Slate Architecture    — classify the day type + identify blowout/high-total
                             games that unlock stacking for their favored team.
  2. Popularity gate       — FADE players (high pre-game media attention) are
                             excluded from the candidate pool.  TARGET/NEUTRAL
                             pass with no bonus.  No RS data involved.
  3. Environmental Advantage — PRIMARY signal: game conditions (Vegas O/U,
                             opposing ERA, bullpen ERA, park, weather, platoon,
                             batting order, moneyline). Groups A/B/C/D.
  4. Individual Explosive Traits — SECONDARY: Statcast kinematics (FB velo/IVB/
                             extension/whiff/chase for SP; avg EV/hard-hit%/
                             barrel% for batters) + K/9 / HR/PA fallback.
  5. Slot Sequencing (Pitcher-Anchor + conditional stacks) — 1 SP pinned to
                             Slot 1 (2.0×); 4 batters fill Slots 2–5 honouring
                             per-team caps that vary by stack eligibility.

EV formula (V10.1 — unchanged from V9.0 at this layer):
    base_ev = env_factor × trait_factor × stack_bonus × dnp_adj × 100

    Starting 5:  base_ev (pure env + trait ranking)
    Moonshot:    base_ev × sharp_bonus × explosive_bonus
                 sharp_bonus    — underground Reddit/FanGraphs analyst buzz (+35% max)
                 explosive_bonus — power_profile / k_rate upside (+20% max)

Stacking (V10.1):
    A team contributes more than one batter ONLY if its game clears both
    is_stack_eligible_game() gates (ML ≤ -200 AND O/U ≥ 9.0).  Every other
    team is capped at one batter per lineup.  See _compute_stack_eligible_teams
    and _team_batter_cap below.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum


from app.core.constants import (
    PARK_HR_FACTORS,
    SLOT_MULTIPLIERS,
    TINY_SLATE_MAX_GAMES,
    PITCHER_DAY_MIN_QUALITY_SP,
    HITTER_DAY_MIN_HIGH_TOTAL,
    HITTER_DAY_VEGAS_TOTAL_THRESHOLD,
    BLOWOUT_MONEYLINE_THRESHOLD,
    BLOWOUT_MIN_GAMES_FOR_STACK_DAY,
    STACK_ELIGIBILITY_VEGAS_TOTAL,
    MIN_SCORE_THRESHOLD,
    PITCHER_ENV_WEAK_OPP_OPS,
    PITCHER_ENV_MIN_K_PER_9,
    MOONSHOT_SHARP_BONUS_MAX,
    MOONSHOT_EXPLOSIVE_BONUS_MAX,
    REQUIRED_PITCHERS_IN_LINEUP,
    PITCHER_ANCHOR_SLOT,
    MAX_PLAYERS_PER_TEAM_BATTERS_STACKABLE,
    MAX_PLAYERS_PER_TEAM_BATTERS_DEFAULT,
    MAX_PLAYERS_PER_GAME_BATTERS,
    is_stack_eligible_game,
    STACK_ELIGIBILITY_SHOOTOUT_TOTAL,
    STACK_BONUS,
    DNP_RISK_PENALTY,
    DNP_UNKNOWN_PENALTY,
    ENV_UNKNOWN_COUNT_THRESHOLD,
    ENV_MODIFIER_FLOOR,
    ENV_MODIFIER_CEILING,
    TRAIT_MODIFIER_FLOOR,
    TRAIT_MODIFIER_CEILING,
    # Graduated env-score scaling thresholds
    PITCHER_ENV_OPS_CEILING,
    PITCHER_ENV_OPS_FLOOR,
    PITCHER_ENV_K_PCT_FLOOR,
    PITCHER_ENV_K_PCT_CEILING,
    PITCHER_ENV_K9_FLOOR,
    PITCHER_ENV_K9_CEILING,
    PITCHER_ENV_PARK_FLOOR,
    PITCHER_ENV_PARK_CEILING,
    PITCHER_ENV_ML_FLOOR,
    PITCHER_ENV_ML_CEILING,
    PITCHER_ENV_MAX_SCORE,
    BATTER_ENV_VEGAS_FLOOR,
    BATTER_ENV_VEGAS_CEILING,
    BATTER_ENV_ERA_FLOOR,
    BATTER_ENV_ERA_CEILING,
    BATTER_ENV_ML_FLOOR,
    BATTER_ENV_ML_CEILING,
    BATTER_ENV_BULLPEN_ERA_FLOOR,
    BATTER_ENV_BULLPEN_ERA_CEILING,
    BATTER_ENV_OPP_WHIP_FLOOR,
    BATTER_ENV_OPP_WHIP_CEILING,
    BATTER_ENV_OPP_WHIP_WEIGHT,
    BATTER_ENV_GROUP_A_SOFT_CAP_POINT,
    BATTER_ENV_GROUP_A_SOFT_CAP_SLOPE,
    BATTER_ENV_PARK_HITTER_FRIENDLY,
    BATTER_ENV_PARK_NEUTRAL,
    BATTER_ENV_WIND_SPEED_MIN,
    BATTER_ENV_WARM_TEMP_THRESHOLD,
    BATTER_ENV_WARM_TEMP_BONUS,
    BATTER_ENV_WIND_OUT_BONUS,
    BATTER_ENV_WIND_OUT_DIRECTIONS,
    BATTER_ENV_WIND_IN_PENALTY,
    BATTER_ENV_WIND_IN_DIRECTIONS,
    BATTER_ENV_MAX_SCORE,
    # Group D — series/momentum context
    SERIES_LEADING_BONUS,
    SERIES_TRAILING_PENALTY,
    TEAM_HOT_L10_THRESHOLD,
    TEAM_COLD_L10_THRESHOLD,
    TEAM_HOT_L10_BONUS,
    TEAM_COLD_L10_PENALTY,
    # Batter env Group C compound (temp × park interaction)
    BATTER_ENV_COMPOUND_HOT_THRESHOLD,
    BATTER_ENV_COMPOUND_COLD_THRESHOLD,
    BATTER_ENV_COMPOUND_PARK_THRESHOLD,
    BATTER_ENV_COMPOUND_BONUS,
    # Volatility amplifier
    BATTER_FORM_VOLATILITY_MAX,
    # Slate classification — quality-SP ERA gate
    QUALITY_SP_ERA_THRESHOLD,
    # V10.5 — pitcher FADE soft penalty (pitchers are kept in pool but discounted)
    PITCHER_FADE_PENALTY,
)
from app.core.utils import BASE_MULTIPLIER, get_trait_score, graduated_scale, graduated_scale_moneyline
from app.services.popularity import PopularityClass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filter 1: Slate Classification (§4.2 Filter 1)
# ---------------------------------------------------------------------------

class SlateType(str, Enum):
    TINY = "tiny"
    PITCHER_DAY = "pitcher_day"
    HITTER_DAY = "hitter_day"
    STANDARD = "standard"


@dataclass
class StackableGame:
    """A game-team pair eligible for mini-stacking.

    A single game may produce up to TWO StackableGame entries:
      - PATH 1 (blowout favorite): one entry, favored_team = the heavy
        moneyline favorite, is_blowout_favorite=True.  Earns STACK_BONUS.
      - PATH 2 (extreme shootout, O/U ≥ STACK_ELIGIBILITY_SHOOTOUT_TOTAL):
        two entries (one per team), is_blowout_favorite=False.  Both teams
        are stack-eligible but neither earns STACK_BONUS — the bonus stays
        gated to true blowout favorites.

    A game can satisfy both paths (e.g., LAD@COL ML=-290, O/U=11.5): the
    favored team gets a PATH 1 entry (with STACK_BONUS) and the opposing
    team gets a PATH 2 entry (stack-eligible, no bonus).
    """
    game_id: int | str | None = None
    favored_team: str = ""
    moneyline: int | None = None
    vegas_total: float | None = None
    opp_starter_era: float | None = None
    is_blowout_favorite: bool = True


@dataclass
class SlateClassification:
    slate_type: SlateType
    game_count: int
    quality_sp_matchups: int = 0
    high_total_games: int = 0
    blowout_games: int = 0
    stackable_games: list[StackableGame] = field(default_factory=list)
    reason: str = ""


def classify_slate(
    game_count: int,
    games: list[dict] | None = None,
) -> SlateClassification:
    """
    Classify the slate BEFORE looking at any individual player.

    §3 Slate Classification:
    - Tiny (1-3 games): limited pool, heavy team-stack
    - Pitcher Day (4+ quality SP matchups): go 4-5 pitchers
    - Hitter/Stack Day (4+ high O/U OR 1+ blowout game): stack the favorite
    - Standard: 2-3P + 2-3 hitters

    Key insight: "Read the slate, don't default to pitchers."
    Hitter/stack days are 38% of slates (most common winning type).
    Blowout games (moneyline ≥ -200) are prime stacking candidates.
    """
    games = games or []

    quality_sp = 0
    high_total = 0
    blowout_games = 0
    stackable: list[StackableGame] = []

    for g in games:
        vt = g.get("vegas_total")
        if vt is not None and vt >= HITTER_DAY_VEGAS_TOTAL_THRESHOLD:
            high_total += 1

        # PATH 1: blowout favorite (moneyline ≤ -200 AND O/U ≥ 9.0).
        # Both gates required — a heavy ML favorite in a low-total pitcher's
        # duel is NOT stack-eligible (correlation upside requires runs).
        # Counts toward `blowout_games` and earns STACK_BONUS.
        home_ml = g.get("home_moneyline")
        away_ml = g.get("away_moneyline")
        home_team = g.get("home_team", "")
        away_team = g.get("away_team", "")
        path1_ou_ok = vt is not None and vt >= STACK_ELIGIBILITY_VEGAS_TOTAL
        if home_ml is not None and home_ml <= BLOWOUT_MONEYLINE_THRESHOLD and path1_ou_ok:
            blowout_games += 1
            stackable.append(StackableGame(
                game_id=g.get("game_id"),
                favored_team=home_team,
                moneyline=home_ml,
                vegas_total=vt,
                opp_starter_era=g.get("away_starter_era"),
                is_blowout_favorite=True,
            ))
        elif away_ml is not None and away_ml <= BLOWOUT_MONEYLINE_THRESHOLD and path1_ou_ok:
            blowout_games += 1
            stackable.append(StackableGame(
                game_id=g.get("game_id"),
                favored_team=away_team,
                moneyline=away_ml,
                vegas_total=vt,
                opp_starter_era=g.get("home_starter_era"),
                is_blowout_favorite=True,
            ))

        # PATH 2: extreme shootout (O/U ≥ SHOOTOUT_TOTAL, ML-agnostic).
        # Both teams become stack-eligible but neither earns STACK_BONUS —
        # they're not heavy favorites, just in a high-run game script.
        if vt is not None and vt >= STACK_ELIGIBILITY_SHOOTOUT_TOTAL:
            already_listed = {
                s.favored_team for s in stackable if s.game_id == g.get("game_id")
            }
            if home_team and home_team not in already_listed:
                stackable.append(StackableGame(
                    game_id=g.get("game_id"),
                    favored_team=home_team,
                    moneyline=home_ml,
                    vegas_total=vt,
                    opp_starter_era=g.get("away_starter_era"),
                    is_blowout_favorite=False,
                ))
            if away_team and away_team not in already_listed:
                stackable.append(StackableGame(
                    game_id=g.get("game_id"),
                    favored_team=away_team,
                    moneyline=away_ml,
                    vegas_total=vt,
                    opp_starter_era=g.get("home_starter_era"),
                    is_blowout_favorite=False,
                ))

        # Check home starter as quality matchup
        h_era = g.get("home_starter_era")
        h_k9 = g.get("home_starter_k_per_9")
        a_ops = g.get("away_team_ops")
        if h_era is not None and h_era < QUALITY_SP_ERA_THRESHOLD:
            if a_ops is not None and a_ops < PITCHER_ENV_WEAK_OPP_OPS:
                quality_sp += 1
            elif h_k9 is not None and h_k9 >= PITCHER_ENV_MIN_K_PER_9:
                quality_sp += 1

        # Check away starter as quality matchup
        a_era = g.get("away_starter_era")
        a_k9 = g.get("away_starter_k_per_9")
        h_ops = g.get("home_team_ops")
        if a_era is not None and a_era < QUALITY_SP_ERA_THRESHOLD:
            if h_ops is not None and h_ops < PITCHER_ENV_WEAK_OPP_OPS:
                quality_sp += 1
            elif a_k9 is not None and a_k9 >= PITCHER_ENV_MIN_K_PER_9:
                quality_sp += 1

    # Sort stackable games by moneyline strength (most negative = biggest favorite)
    stackable.sort(key=lambda s: s.moneyline if s.moneyline is not None else 0)

    # Classification logic — check hitter/stack BEFORE pitcher day
    # because hitter/stack days are 38% vs pitcher days 23%
    if game_count <= TINY_SLATE_MAX_GAMES:
        return SlateClassification(
            slate_type=SlateType.TINY,
            game_count=game_count,
            quality_sp_matchups=quality_sp,
            high_total_games=high_total,
            blowout_games=blowout_games,
            stackable_games=stackable,
            reason=f"Tiny slate ({game_count} games). Stack the favorite.",
        )

    # Blowout games trigger hitter/stack day even without high O/U counts
    if (high_total >= HITTER_DAY_MIN_HIGH_TOTAL
            or blowout_games >= BLOWOUT_MIN_GAMES_FOR_STACK_DAY):
        stack_reason = []
        if high_total >= HITTER_DAY_MIN_HIGH_TOTAL:
            stack_reason.append(f"{high_total} high O/U games")
        if blowout_games > 0:
            teams = [s.favored_team for s in stackable]
            stack_reason.append(f"{blowout_games} blowout game(s): {', '.join(teams)}")
        return SlateClassification(
            slate_type=SlateType.HITTER_DAY,
            game_count=game_count,
            quality_sp_matchups=quality_sp,
            high_total_games=high_total,
            blowout_games=blowout_games,
            stackable_games=stackable,
            reason=f"Hitter/Stack day: {'; '.join(stack_reason)}. Stack 3-4 from favorite + 1-2 diversifiers.",
        )

    if quality_sp >= PITCHER_DAY_MIN_QUALITY_SP:
        return SlateClassification(
            slate_type=SlateType.PITCHER_DAY,
            game_count=game_count,
            quality_sp_matchups=quality_sp,
            high_total_games=high_total,
            blowout_games=blowout_games,
            stackable_games=stackable,
            reason=f"Pitcher day: {quality_sp} quality SP matchups. Go 4-5 pitchers.",
        )

    return SlateClassification(
        slate_type=SlateType.STANDARD,
        game_count=game_count,
        quality_sp_matchups=quality_sp,
        high_total_games=high_total,
        blowout_games=blowout_games,
        stackable_games=stackable,
        reason=f"Standard slate ({game_count} games). 2-3 pitchers + 2-3 hitters.",
    )


# ---------------------------------------------------------------------------
# Filter 2: Environmental Advantage (§4.2 Filter 2)
# ---------------------------------------------------------------------------

@dataclass
class EnvironmentalProfile:
    """Pre-game environmental factors for a single player."""
    player_name: str
    team: str
    position: str
    is_pitcher: bool = False
    env_score: float = 0.5  # 0-1.0; >0.5 = passes environmental filter
    env_factors: list[str] = field(default_factory=list)
    env_unknown_count: int = 0  # how many factors were missing (unknown vs bad)

    # Pitcher-specific
    opp_team_ops: float | None = None
    opp_team_k_pct: float | None = None
    pitcher_k_per_9: float | None = None
    park_factor: float | None = None
    is_home: bool = False

    # Batter-specific
    vegas_total: float | None = None
    opp_pitcher_era: float | None = None
    platoon_advantage: bool = False
    batting_order: int | None = None



def compute_pitcher_env_score(
    opp_team_ops: float | None = None,
    opp_team_k_pct: float | None = None,
    pitcher_k_per_9: float | None = None,
    park_team: str | None = None,
    is_home: bool = False,
    team_moneyline: int | None = None,
) -> tuple[float, list[str]]:
    """
    Compute environmental score for a pitcher (0-1.0).

    V8.0: Added moneyline as a factor — Win bonus probability is a major
    pitcher RS component.  Thresholds are graduated (linear interpolation)
    to avoid false-precision cliffs on small April samples.

    Factors:
    1. Weak opponent offense (OPS)       — graduated 0.780→0, 0.650→1.0
    2. High-K opponent (team K%)         — graduated 0.20→0, 0.26→1.0
    3. K upside (pitcher's own K/9)      — graduated 6.0→0, 10.0→1.0
    4. Pitcher-friendly park             — graduated 1.05→0, 0.90→1.0
    5. Moneyline favorite (Win bonus)    — graduated -110→0, -250→1.0
    6. Home field                        — 0.5
    """
    score = 0.0
    factors = []
    max_score = PITCHER_ENV_MAX_SCORE

    # 1. Weak opponent offense — graduated (lower OPS = better for pitcher)
    if opp_team_ops is not None:
        contrib = graduated_scale(opp_team_ops, PITCHER_ENV_OPS_CEILING, PITCHER_ENV_OPS_FLOOR)
        score += contrib
        if contrib > 0:
            label = "Weak" if contrib >= 0.9 else "Below-avg"
            factors.append(f"{label} opponent OPS ({opp_team_ops:.3f})")

    # 2. High-K opponent — graduated (higher K% = better for pitcher)
    if opp_team_k_pct is not None:
        contrib = graduated_scale(opp_team_k_pct, PITCHER_ENV_K_PCT_FLOOR, PITCHER_ENV_K_PCT_CEILING)
        score += contrib
        if contrib > 0:
            label = "High-K" if contrib >= 0.9 else "Above-avg K"
            factors.append(f"{label} opponent ({opp_team_k_pct:.1%})")

    # 3. K upside (pitcher's own K/9) — graduated (higher = better)
    if pitcher_k_per_9 is not None:
        contrib = graduated_scale(pitcher_k_per_9, PITCHER_ENV_K9_FLOOR, PITCHER_ENV_K9_CEILING)
        score += contrib
        if contrib > 0:
            label = "Elite K upside" if contrib >= 0.9 else "K upside"
            factors.append(f"{label} (K/9={pitcher_k_per_9:.1f})")

    # 4. Pitcher-friendly park — graduated (lower park factor = better)
    if park_team:
        pf = PARK_HR_FACTORS.get(park_team, 1.0)
        contrib = graduated_scale(pf, PITCHER_ENV_PARK_CEILING, PITCHER_ENV_PARK_FLOOR)
        score += contrib
        if contrib > 0:
            label = "Pitcher-friendly" if contrib >= 0.9 else "Neutral-to-friendly"
            factors.append(f"{label} park ({park_team}, factor={pf:.2f})")

    # 5. Moneyline favorite — graduated (more negative = stronger favorite)
    if team_moneyline is not None:
        contrib = graduated_scale_moneyline(team_moneyline, PITCHER_ENV_ML_FLOOR, PITCHER_ENV_ML_CEILING)
        score += contrib
        if contrib > 0:
            label = "Heavy favorite" if contrib >= 0.9 else "Favorite"
            factors.append(f"{label} (ML={team_moneyline}) — Win bonus likely")

    # 6. Home field
    if is_home:
        score += 0.5
        factors.append("Home field")

    env_score = min(1.0, score / max_score)
    return env_score, factors


def compute_batter_env_score(
    vegas_total: float | None = None,
    opp_pitcher_era: float | None = None,
    platoon_advantage: bool = False,
    batting_order: int | None = None,
    park_team: str | None = None,
    wind_speed_mph: float | None = None,
    wind_direction: str | None = None,
    temperature_f: int | None = None,
    team_moneyline: int | None = None,
    opp_bullpen_era: float | None = None,
    series_team_wins: int | None = None,
    series_opp_wins: int | None = None,
    team_l10_wins: int | None = None,
    opp_starter_whip: float | None = None,
) -> tuple[float, list[str], int]:
    """
    Compute environmental score for a batter (0-1.0).

    V8.0 changes:
    - Correlated run-environment signals (O/U, ERA, moneyline, bullpen)
      grouped and capped at 2.0 to prevent redundancy inflation.
    - Batting order graduated (1-9 scale) with neutral baseline for unknowns.
    - All thresholds graduated via linear interpolation.

    V8.1 changes (April 15):
    - Group D added: series/momentum context (series wins, recent L10 form).
      Addresses "correctly-avoided player disguised as a ghost" problem where
      a low-media batter gets TARGET classification despite a terrible matchup.

    Returns a third value, `unknown_count`, tracking how many environmental
    factors were missing (None) vs. confirmed bad.

    Signal groups:
      Group A — Run environment (O/U, ERA, moneyline, bullpen) — capped 2.0
      Group B — Player situation (platoon, batting order) — up to 2.0
      Group C — Venue (park + weather) — up to 1.0
      Group D — Series/momentum (series record, recent L10) — ±0.8
    """
    factors: list[str] = []
    unknown_count = 0

    # ---------------------------------------------------------------
    # Group A: Run environment (correlated signals — capped at 2.0)
    # Vegas O/U, opposing ERA, moneyline, and bullpen ERA all measure
    # the same underlying condition: "this team will score runs."
    # Capping at 2.0 prevents 4 redundant signals from inflating env.
    # ---------------------------------------------------------------
    run_env = 0.0

    # A1. Vegas O/U — graduated
    if vegas_total is not None:
        contrib = graduated_scale(vegas_total, BATTER_ENV_VEGAS_FLOOR, BATTER_ENV_VEGAS_CEILING)
        run_env += contrib
        if contrib > 0:
            label = "High-run environment" if contrib >= 0.9 else "Run environment"
            factors.append(f"{label} (O/U={vegas_total:.1f})")
    else:
        unknown_count += 1

    # A2. Weak opposing starter — graduated
    if opp_pitcher_era is not None:
        contrib = graduated_scale(opp_pitcher_era, BATTER_ENV_ERA_FLOOR, BATTER_ENV_ERA_CEILING)
        run_env += contrib
        if contrib > 0:
            label = "Weak opposing starter" if contrib >= 0.9 else "Vulnerable starter"
            factors.append(f"{label} (ERA={opp_pitcher_era:.2f})")
    else:
        unknown_count += 1

    # A3. Moneyline favorite — graduated
    if team_moneyline is not None:
        contrib = graduated_scale_moneyline(team_moneyline, BATTER_ENV_ML_FLOOR, BATTER_ENV_ML_CEILING)
        run_env += contrib
        if contrib > 0:
            label = "Heavy favorite" if contrib >= 0.9 else "Moneyline favorite"
            factors.append(f"{label} (ML={team_moneyline})")
    else:
        unknown_count += 1

    # A4. Vulnerable bullpen — graduated
    if opp_bullpen_era is not None:
        contrib = graduated_scale(opp_bullpen_era, BATTER_ENV_BULLPEN_ERA_FLOOR, BATTER_ENV_BULLPEN_ERA_CEILING)
        run_env += contrib
        if contrib > 0:
            label = "Vulnerable bullpen" if contrib >= 0.9 else "Below-avg bullpen"
            factors.append(f"{label} (ERA={opp_bullpen_era:.2f})")
    else:
        unknown_count += 1

    # A5. Opposing starter WHIP — graduated, weighted at half of ERA's contribution.
    # WHIP correlates with ERA (r=0.816 across 33 historical slates) but adds modest
    # independent signal in the corners where ERA is misleading (one-bad-start
    # inflation, low-ERA starter with poor command stats).  Weight cap at 0.5
    # acknowledges the correlation: most signal already lives in ERA.  The Group A
    # soft cap then absorbs any remaining redundancy when ERA and WHIP agree.
    if opp_starter_whip is not None:
        contrib = BATTER_ENV_OPP_WHIP_WEIGHT * graduated_scale(
            opp_starter_whip, BATTER_ENV_OPP_WHIP_FLOOR, BATTER_ENV_OPP_WHIP_CEILING
        )
        run_env += contrib
        if contrib > 0:
            label = "Vulnerable starter command" if contrib >= 0.45 else "Below-avg starter command"
            factors.append(f"{label} (WHIP={opp_starter_whip:.2f})")
    else:
        unknown_count += 1

    # Soft cap: first 2.0 of correlated-signal sum is taken full; sum above 2.0
    # contributes at 25% slope.  Preserves a little upside for "perfect storm"
    # games (all 4 signals lit) without letting redundant signals multiply
    # linearly.  Previous hard cap at 2.0 was discarding all signal above the
    # saturation point; the soft cap keeps a small share of it.
    if run_env > BATTER_ENV_GROUP_A_SOFT_CAP_POINT:
        excess = run_env - BATTER_ENV_GROUP_A_SOFT_CAP_POINT
        run_env = BATTER_ENV_GROUP_A_SOFT_CAP_POINT + excess * BATTER_ENV_GROUP_A_SOFT_CAP_SLOPE

    # ---------------------------------------------------------------
    # Group B: Player situation (independent signals — up to 2.0)
    # ---------------------------------------------------------------
    situation = 0.0

    # B1. Platoon advantage (binary — either you have it or you don't)
    if platoon_advantage:
        situation += 1.0
        factors.append("Platoon advantage")

    # B2. Batting order — graduated scale.  Removes the hard top-5 gate that
    #     structurally excluded ghost players.  Unknown orders contribute 0
    #     (no mathematical guessing); missing-data risk is accounted for via
    #     the DNP adjustment, not here.
    if batting_order is not None:
        if batting_order <= 3:
            situation += 1.0
            factors.append(f"Premium lineup spot (bats #{batting_order})")
        elif batting_order <= 5:
            situation += 0.75
            factors.append(f"Top of lineup (bats #{batting_order})")
        elif batting_order <= 7:
            situation += 0.50
            factors.append(f"Middle of lineup (bats #{batting_order})")
        else:
            situation += 0.25
            factors.append(f"Bottom of lineup (bats #{batting_order})")
    else:
        # Unknown batting order contributes 0 to env — no mathematical
        # guessing ("assume they bat 6th/7th").  DNP risk for unpublished
        # lineups is handled separately by _compute_dnp_adjustment() via
        # DNP_UNKNOWN_PENALTY, so this branch avoids double-counting the
        # missing-data penalty while staying faithful to the no-fallback rule.
        unknown_count += 1

    # ---------------------------------------------------------------
    # Group C: Venue — park + weather (capped at 1.0)
    # ---------------------------------------------------------------
    venue = 0.0
    if park_team:
        pf = PARK_HR_FACTORS.get(park_team, 1.0)
        if pf >= BATTER_ENV_PARK_HITTER_FRIENDLY:
            venue = 1.0
            factors.append(f"Hitter-friendly park ({park_team}, factor={pf:.2f})")
        elif pf >= BATTER_ENV_PARK_NEUTRAL:
            venue = 0.5

    if wind_speed_mph is not None and wind_speed_mph >= BATTER_ENV_WIND_SPEED_MIN and wind_direction:
        direction_upper = wind_direction.upper()
        if any(d in direction_upper for d in BATTER_ENV_WIND_OUT_DIRECTIONS):
            venue = min(1.0, venue + BATTER_ENV_WIND_OUT_BONUS)
            factors.append(f"Wind blowing out ({wind_speed_mph:.0f} mph)")
        # V10.3 (Apr 27): symmetric wind IN penalty.  Previously wind blowing in
        # was treated identical to neutral cross-wind.  HV rate analysis shows IN
        # suppresses HV by ~2.2pts vs neutral baseline (45.8% vs 48.0%), about
        # half the magnitude of OUT's +4.9pt boost — hence half the magnitude of
        # the OUT bonus.  Floor at 0.0 matches the existing cold+pitcher-park
        # compound penalty pattern.
        elif any(d in direction_upper for d in BATTER_ENV_WIND_IN_DIRECTIONS):
            venue = max(0.0, venue - BATTER_ENV_WIND_IN_PENALTY)
            factors.append(f"Wind blowing in ({wind_speed_mph:.0f} mph)")

    if temperature_f is not None and temperature_f >= BATTER_ENV_WARM_TEMP_THRESHOLD:
        venue = min(1.0, venue + BATTER_ENV_WARM_TEMP_BONUS)
        factors.append(f"Warm conditions ({temperature_f}°F)")

    # Compound signal: hot day at hitter park or cold day at pitcher park
    if temperature_f is not None and park_team:
        pf = PARK_HR_FACTORS.get(park_team, 1.0)
        if temperature_f > BATTER_ENV_COMPOUND_HOT_THRESHOLD and pf > BATTER_ENV_COMPOUND_PARK_THRESHOLD:
            venue = min(1.0, venue + BATTER_ENV_COMPOUND_BONUS)
            factors.append(f"Hot+hitter park synergy ({temperature_f}°F at {park_team})")
        elif temperature_f < BATTER_ENV_COMPOUND_COLD_THRESHOLD and pf < BATTER_ENV_COMPOUND_PARK_THRESHOLD:
            venue = max(0.0, venue - BATTER_ENV_COMPOUND_BONUS)
            factors.append(f"Cold+pitcher park synergy ({temperature_f}°F at {park_team})")

    # ---------------------------------------------------------------
    # Group D: Series/Momentum context (±0.8 additive)
    # Addresses the "correctly-avoided player disguised as a ghost"
    # problem: a batter on a team getting swept faces genuinely bad
    # conditions regardless of low media attention.
    # ---------------------------------------------------------------
    momentum = 0.0
    if series_team_wins is not None and series_opp_wins is not None:
        series_deficit = series_opp_wins - series_team_wins
        series_lead = series_team_wins - series_opp_wins
        if series_lead >= 2:
            momentum += SERIES_LEADING_BONUS
            factors.append(f"Leading series {series_team_wins}-{series_opp_wins}")
        elif series_deficit >= 2:
            momentum -= SERIES_TRAILING_PENALTY
            factors.append(f"Trailing series {series_team_wins}-{series_opp_wins} (sweep risk)")

    if team_l10_wins is not None:
        if team_l10_wins >= TEAM_HOT_L10_THRESHOLD:
            momentum += TEAM_HOT_L10_BONUS
            factors.append(f"Hot team (L10: {team_l10_wins}-{10 - team_l10_wins})")
        elif team_l10_wins <= TEAM_COLD_L10_THRESHOLD:
            momentum -= TEAM_COLD_L10_PENALTY
            factors.append(f"Cold team (L10: {team_l10_wins}-{10 - team_l10_wins})")

    # ---------------------------------------------------------------
    # Final score: sum of capped groups / max_score
    # ---------------------------------------------------------------
    total = run_env + situation + venue + momentum
    max_score = BATTER_ENV_MAX_SCORE

    if unknown_count > 0:
        factors.append(f"{unknown_count} unknown factor(s) (data scarcity, not bad env)")

    env_score = min(1.0, total / max_score)
    return env_score, factors, unknown_count



# ---------------------------------------------------------------------------
# Filter 4+5: Boost Optimization & Lineup Construction
# These are integrated into the FilterStrategyOptimizer below.
# ---------------------------------------------------------------------------

@dataclass
class FilteredCandidate:
    """A player card that has passed through Filters 1-3.

    ========================================================================
    V10.0 STRUCTURAL ISOLATION: card_boost and drafts do NOT exist on this
    dataclass.  Removing them entirely (instead of marking them "display only")
    eliminates the temptation to reference them in EV logic and makes the
    "no ownership/boost signals in pre-game prediction" rule enforceable by
    static grep.  The router layer reads card_boost and drafts straight from
    the source FilterCard for display purposes.
    ========================================================================

    EV is computed from pre-game signals only:
      env_score   — Vegas O/U, opposing ERA/bullpen, park, weather, platoon, batting order
      total_score — season-level trait quality (K/9, ISO, barrel%, speed, recent form)
      popularity  — media attention (Google Trends, ESPN, Reddit) — NOT platform ownership

    See CLAUDE.md § "Signal Isolation: ABSOLUTE RULE" for full rationale.
    """
    player_name: str
    team: str
    position: str
    total_score: float    # 0-100 from scoring engine (pre-game season stats)
    env_score: float      # 0-1.0 from environmental filter (pre-game conditions)
    env_factors: list[str] = field(default_factory=list)
    env_unknown_count: int = 0  # how many env factors were missing data
    popularity: PopularityClass = PopularityClass.NEUTRAL  # web-scraped (pre-game)
    game_id: int | str | None = None  # for diversification tracking
    is_pitcher: bool = False
    sharp_score: float = 0.0
    traits: list = field(default_factory=list)  # TraitScore list from scoring engine
    batting_order: int | None = None  # 1-9 if confirmed in lineup, None = DNP risk
    is_in_blowout_game: bool = False  # set by run_filter_strategy before EV computation

    # Series/momentum context — populated from SlateGame series fields.
    # Used by Group D env scoring and the momentum gate in _compute_base_ev().
    # None = data unavailable (treated as neutral, no penalty).
    series_team_wins: int | None = None   # wins by this player's team in current series
    series_opp_wins: int | None = None    # wins by the opponent in current series
    team_l10_wins: int | None = None      # this team's wins in last 10 games

    # Two-way player detection: True if stored as non-pitcher (e.g., DH) but detected as a
    # confirmed starter (e.g., Ohtani pitching). Used to annotate the candidate in outputs
    # so users understand why position ≠ slot assignment.
    is_two_way_pitcher: bool = False

    # Computed by the optimizer
    filter_ev: float = 0.0


@dataclass
class FilterSlotAssignment:
    slot_index: int
    slot_mult: float
    candidate: FilteredCandidate
    # Slot-weighted ranking signal: filter_ev × (slot_mult / 2.0).
    # This is NOT an RS prediction — it is a relative ranking score used
    # to order and compare lineups.  It has no units in common with RS.
    expected_slot_value: float


@dataclass
class FilterOptimizedLineup:
    slots: list[FilterSlotAssignment]
    # Sum of slot-weighted ranking signals.  Used only for lineup comparison,
    # not as an RS or total_value prediction.
    total_expected_value: float
    strategy: str
    slate_classification: SlateClassification
    composition: dict = field(default_factory=dict)  # {pitchers: N, hitters: N}
    warnings: list[str] = field(default_factory=list)


def _compute_dnp_adjustment(candidate: FilteredCandidate) -> float:
    """Compute DNP risk adjustment for batters only.

    Pitchers at this stage are already filtered to confirmed probable starters.
    For batters: a known batting_order = posted lineup, full confidence.
    Missing batting_order with many other env fields missing = slate data
    not yet published (DNP-unknown, light penalty).  Missing batting_order
    with a full env context = lineup posted without the player (DNP-confirmed,
    heavier penalty).
    """
    if candidate.is_pitcher or candidate.batting_order is not None:
        return 1.0
    if candidate.env_unknown_count >= ENV_UNKNOWN_COUNT_THRESHOLD:
        return DNP_UNKNOWN_PENALTY
    return DNP_RISK_PENALTY


def _compute_base_ev(candidate: FilteredCandidate) -> float:
    """Compute the shared base EV used by both Starting 5 and Moonshot.

    V9.0 "Filter, Not Forecast" — EV is built exclusively from pre-game
    signals.  No RS data, no historical outcomes, no ownership counts.
    FADE players are excluded from the candidate pool before this runs.

    Two signals:
      1. env_factor   — PRIMARY: game conditions (Vegas O/U, opposing ERA,
                        bullpen ERA, park, weather, platoon, batting order,
                        moneyline).  Range: 0.70–1.30 (1.86× swing).
      2. trait_factor — SECONDARY: intrinsic player quality (K/9, ISO,
                        barrel%, SB pace, ERA, WHIP, recent form, 0-100).
                        Range: 0.85–1.15 (1.35× swing).

    Plus contextual multipliers: stack_bonus, dnp_adj, volatility_amplifier.

    Formula:
        base_ev = env_factor × volatility_amplifier × trait_factor × stack_bonus × dnp_adj × 100
    """
    raw_env = max(candidate.env_score, 0.0)
    env_factor = ENV_MODIFIER_FLOOR + raw_env * (ENV_MODIFIER_CEILING - ENV_MODIFIER_FLOOR)
    env_factor = max(ENV_MODIFIER_FLOOR, min(ENV_MODIFIER_CEILING, env_factor))

    # Volatility amplifier: high-variance players amplify env conditions (both good and bad).
    # Recent form CV is only available for batters; pitchers default to 1.0.
    volatility_amplifier = 1.0
    if candidate.traits:
        for trait in candidate.traits:
            if trait.name == "recent_form" and "recent_form_cv" in trait.metadata:
                cv = trait.metadata["recent_form_cv"]
                volatility_amplifier = 1.0 + (cv * BATTER_FORM_VOLATILITY_MAX)
                break

    trait_floor = MIN_SCORE_THRESHOLD / 100.0
    raw_trait = max(candidate.total_score, float(MIN_SCORE_THRESHOLD)) / 100.0
    trait_factor = TRAIT_MODIFIER_FLOOR + (raw_trait - trait_floor) * (
        TRAIT_MODIFIER_CEILING - TRAIT_MODIFIER_FLOOR
    ) / (1.0 - trait_floor)
    trait_factor = max(TRAIT_MODIFIER_FLOOR, min(TRAIT_MODIFIER_CEILING, trait_factor))

    stack_bonus = STACK_BONUS if candidate.is_in_blowout_game else 1.0
    dnp_adj = _compute_dnp_adjustment(candidate)

    # V10.5: FADE pitchers stay in the pool but pay a soft penalty.
    # Batters are still excluded entirely at the popularity gate, so a
    # FADE candidate that survives EV must be a pitcher.  The 15% haircut
    # is small enough that a genuinely strong pitcher (good env + good
    # traits) can still beat the field — the gate only sorts ties.
    pitcher_pop_penalty = (
        PITCHER_FADE_PENALTY
        if candidate.is_pitcher and candidate.popularity == PopularityClass.FADE
        else 1.0
    )

    return (
        env_factor
        * volatility_amplifier
        * trait_factor
        * stack_bonus
        * dnp_adj
        * pitcher_pop_penalty
        * 100.0
    )


def _compute_filter_ev(candidate: FilteredCandidate) -> float:
    """Compute Starting 5 EV: env × trait × context (no RS data)."""
    return _compute_base_ev(candidate)


def _compute_stack_eligible_teams(slate_class: SlateClassification) -> set[str]:
    """Return the set of team abbreviations allowed to contribute a stack today.

    A team is stack-eligible only if its game satisfies BOTH the blowout
    moneyline gate AND the high-total Vegas O/U gate (see is_stack_eligible_game
    in app/core/constants.py).  Only teams on the favored side of qualifying
    games are returned; every other team falls back to 1-batter-per-team in
    the composition phase.
    """
    eligible: set[str] = set()
    for sg in slate_class.stackable_games:
        if is_stack_eligible_game(sg.moneyline, sg.vegas_total) and sg.favored_team:
            eligible.add(sg.favored_team.upper())
    return eligible


def _team_batter_cap(team: str, stack_eligible_teams: set[str]) -> int:
    """Max batters from a given team for today's lineup.

    Conservative default: 1 batter per team.  Only teams that cleared the
    is_stack_eligible_game gate may contribute up to 4.
    """
    if team.upper() in stack_eligible_teams:
        return MAX_PLAYERS_PER_TEAM_BATTERS_STACKABLE
    return MAX_PLAYERS_PER_TEAM_BATTERS_DEFAULT


def _fill_batter_slots(
    ordered_batters: list[FilteredCandidate],
    anchor_game_id: int | str | None,
    anchor_team: str,
    stack_eligible_teams: set[str],
    slots_to_fill: int = 4,
) -> list[FilteredCandidate]:
    """Fill `slots_to_fill` batter slots from an EV-ordered batter pool.

    Applies the anti-correlation guard, per-team cap, and per-game cap.
    Pool must already be sorted by filter_ev descending and must exclude pitchers.

    `slots_to_fill` defaults to 4 (the 1P+4B path); the V10.5 pure-batter
    path passes 5 to fill the entire lineup with no anchor restrictions
    (caller passes anchor_game_id=None, anchor_team="" to disable that guard).
    """
    batters: list[FilteredCandidate] = []
    team_count: dict[str, int] = {}
    game_count: dict[int | str, int] = {}

    for c in ordered_batters:
        if len(batters) == slots_to_fill:
            break
        team_key = c.team.upper()
        if anchor_game_id is not None and c.game_id == anchor_game_id and team_key != anchor_team:
            continue
        cap = _team_batter_cap(team_key, stack_eligible_teams)
        if team_count.get(team_key, 0) >= cap:
            continue
        if c.game_id is not None and game_count.get(c.game_id, 0) >= MAX_PLAYERS_PER_GAME_BATTERS:
            continue
        team_count[team_key] = team_count.get(team_key, 0) + 1
        if c.game_id is not None:
            game_count[c.game_id] = game_count.get(c.game_id, 0) + 1
        batters.append(c)

    return batters


def _lineup_total_ev(lineup: list[FilteredCandidate]) -> float:
    """Compute the slot-weighted total EV for a candidate ordering.

    Mirrors `_smart_slot_assignment`: highest-EV candidate gets slot 1
    (2.0×); subsequent candidates fill slots 2-5 by EV descending.  Pitchers
    are pinned to slot 1 if present.  This is the comparison metric used to
    pick between 1P+4B and 0P+5B variants — see `_build_best_lineup_variant`.
    """
    if not lineup:
        return 0.0
    pitcher = next((c for c in lineup if c.is_pitcher), None)
    batters = sorted(
        [c for c in lineup if not c.is_pitcher],
        key=lambda c: c.filter_ev,
        reverse=True,
    )
    slot_mults_desc = sorted(SLOT_MULTIPLIERS.values(), reverse=True)
    total = 0.0
    if pitcher is not None:
        total += pitcher.filter_ev * (slot_mults_desc[0] / BASE_MULTIPLIER)
        remaining_mults = slot_mults_desc[1:]
    else:
        remaining_mults = slot_mults_desc
    for batter, mult in zip(batters, remaining_mults):
        total += batter.filter_ev * (mult / BASE_MULTIPLIER)
    return total


def _build_pure_batter_lineup(
    candidates: list[FilteredCandidate],
    slate_class: SlateClassification,
) -> list[FilteredCandidate]:
    """Build a 0P+5B lineup: top-5 batters by filter_ev under team/game caps.

    No anchor pitcher → no opposing-side restriction.  Returns [] if fewer
    than 5 batters can be assembled under the caps (extremely rare in
    practice; ~30 teams × cap of 1 + a couple stack-eligible teams × 2
    means we typically have 30+ legal batter slots).  An empty return
    signals the caller to fall back to the 1P+4B path.
    """
    ordered_batters = sorted(
        [c for c in candidates if not c.is_pitcher],
        key=lambda c: c.filter_ev,
        reverse=True,
    )
    stack_eligible_teams = _compute_stack_eligible_teams(slate_class)
    batters = _fill_batter_slots(
        ordered_batters,
        anchor_game_id=None,
        anchor_team="",
        stack_eligible_teams=stack_eligible_teams,
        slots_to_fill=5,
    )
    if len(batters) < 5:
        return []
    return batters


def _enforce_composition(
    candidates: list[FilteredCandidate],
    slate_class: SlateClassification,
) -> list[FilteredCandidate]:
    """
    V10.5 EV-driven composition: 0 OR 1 pitcher, EV decides.

    Builds the standard 1P+4B (anchor) variant AND a 0P+5B (pure-batter)
    variant, then returns the higher slot-weighted total.  This lets shootout
    slates — where 4 of yesterday's top 5 winning lineups had zero pitchers —
    naturally surface a 5-batter lineup, while heavy-pitcher days still
    return the anchored shape because the pitcher's slot-1 multiplier (2.0×)
    keeps it competitive when his EV beats the marginal batter.

    Stacking gates and per-team / per-game caps apply identically to both
    variants — the only difference is whether slot 1 is taken by a pitcher
    or by the highest-EV batter.

    Anchor variant construction (unchanged from V10.1):
    1. Select highest-EV pitcher.
    2. NEVER draft an opposing batter in his game (pitcher ↔ hitter
       negative correlation).  Teammates allowed within stack caps.
    3. Fill 4 batter slots by filter_ev descending under team + game caps.

    Pure-batter variant: top-5 batters by filter_ev, no anchor restriction.

    Tiebreak: pitcher variant wins exact ties so we keep the conservative
    shape unless the 5B EV truly dominates.
    """
    all_sorted = sorted(candidates, key=lambda c: c.filter_ev, reverse=True)

    stack_eligible_teams = _compute_stack_eligible_teams(slate_class)
    ordered_batters = [c for c in all_sorted if not c.is_pitcher]

    # Variant A: 1P + 4B (pitcher-anchored).  Build only if a pitcher exists.
    anchor_pitcher = next((c for c in all_sorted if c.is_pitcher), None)
    anchor_lineup: list[FilteredCandidate] = []
    if anchor_pitcher is not None:
        anchor_game_id = anchor_pitcher.game_id
        anchor_team = anchor_pitcher.team.upper()
        anchor_lineup = [anchor_pitcher]
        anchor_lineup.extend(
            _fill_batter_slots(ordered_batters, anchor_game_id, anchor_team, stack_eligible_teams)
        )
        anchor_lineup = _validate_lineup_structure(
            anchor_lineup, ordered_batters, anchor_pitcher=anchor_pitcher,
            stack_eligible_teams=stack_eligible_teams,
        )

    # Variant B: 0P + 5B (pure-batter).
    pure_batter_lineup = _build_pure_batter_lineup(candidates, slate_class)
    if pure_batter_lineup:
        pure_batter_lineup = _validate_lineup_structure(
            pure_batter_lineup, ordered_batters, anchor_pitcher=None,
            stack_eligible_teams=stack_eligible_teams,
        )

    # Tiebreak: pitcher variant wins ties (>=, not >) — conservative default
    # keeps the strategy doc's pitcher-anchor identity unless 5B truly dominates.
    anchor_ev = _lineup_total_ev(anchor_lineup) if len(anchor_lineup) == 5 else -1.0
    pure_ev = _lineup_total_ev(pure_batter_lineup) if len(pure_batter_lineup) == 5 else -1.0

    if anchor_ev < 0 and pure_ev < 0:
        raise ValueError(
            "Candidate pool produced neither a 1P+4B nor a 0P+5B lineup. "
            "Cannot build a lineup."
        )

    if anchor_ev >= pure_ev:
        chosen, label = anchor_lineup, "1P/4H"
    else:
        chosen, label = pure_batter_lineup, "0P/5H"

    from collections import Counter
    stack_teams = [t for t, n in Counter(c.team for c in chosen if not c.is_pitcher).items() if n >= 2]
    anchor_name = anchor_pitcher.player_name if anchor_pitcher is not None else "—"
    logger.info(
        "V10.5 composition: chose %s (anchor_ev=%.2f, pure_ev=%.2f) — anchor=%s "
        "stack_eligible=%s mini_stacks_used=%s (candidates: %d)",
        label, anchor_ev, pure_ev, anchor_name,
        sorted(stack_eligible_teams) or "none",
        stack_teams or "none", len(candidates),
    )
    return chosen


def _validate_lineup_structure(
    lineup: list[FilteredCandidate],
    all_candidates_sorted: list[FilteredCandidate],
    anchor_pitcher: FilteredCandidate | None = None,
    stack_eligible_teams: set[str] | None = None,
) -> list[FilteredCandidate]:
    """V10.1 validation — enforces:

      1. Exactly 1 pitcher (anchor).
      2. No batter from the opposing side of the anchor's game.
      3. Per-team batter cap: stack-eligible teams allow up to 2, all others 1.
      4. Per-game batter cap: at most 2 batters from any single game.

    Anchor-teammate batters are explicitly allowed (within caps).  The only
    absolute game-level prohibition is opposing batters against our own SP.
    """
    if len(lineup) < 5:
        return lineup

    anchor_team = anchor_pitcher.team.upper() if anchor_pitcher else None
    anchor_game = anchor_pitcher.game_id if anchor_pitcher else None
    stack_eligible_teams = stack_eligible_teams or set()

    anchor_idx: int | None = None
    if anchor_pitcher is not None:
        for i, c in enumerate(lineup):
            if c.player_name == anchor_pitcher.player_name:
                anchor_idx = i
                break

    def _protected(idx: int) -> bool:
        return anchor_idx is not None and idx == anchor_idx

    from collections import Counter

    # Rule 1: Never an opposing batter in the anchor's game.
    if anchor_game is not None and anchor_team is not None:
        violator_indices = [
            i for i, c in enumerate(lineup)
            if not _protected(i)
            and c.game_id == anchor_game
            and c.team.upper() != anchor_team
        ]
        lineup_names = {c.player_name for c in lineup}
        for idx in violator_indices:
            replacement = next(
                (c for c in all_candidates_sorted
                 if c.player_name not in lineup_names
                 and not c.is_pitcher
                 and not (c.game_id == anchor_game and c.team.upper() != anchor_team)),
                None,
            )
            if replacement:
                removed_name = lineup[idx].player_name
                lineup_names.discard(removed_name)
                lineup[idx] = replacement
                lineup_names.add(replacement.player_name)
                logger.info(
                    "Anchor-opposition cap: replaced %s (opponent of %s) with %s",
                    removed_name, anchor_pitcher.player_name, replacement.player_name,
                )

    # Rule 2: Per-team batter cap — conservative default 1, lifts to 2
    # only for teams that cleared the is_stack_eligible_game gate.
    def _batter_team_counts(current):
        return Counter(c.team for c in current if not c.is_pitcher)

    def _batter_game_counts(current):
        return Counter(c.game_id for c in current if not c.is_pitcher and c.game_id is not None)

    batter_counts = _batter_team_counts(lineup)
    for team, count in batter_counts.items():
        team_cap = _team_batter_cap(team, stack_eligible_teams)
        if count > team_cap:
            team_indices = sorted(
                [i for i, c in enumerate(lineup)
                 if not _protected(i) and c.team == team],
                key=lambda i: lineup[i].filter_ev,
                reverse=True,
            )
            lineup_names = {c.player_name for c in lineup}
            for idx in team_indices[team_cap:]:
                current_counts = _batter_team_counts(lineup)
                current_game_counts = _batter_game_counts(lineup)
                replacement = next(
                    (c for c in all_candidates_sorted
                     if c.player_name not in lineup_names
                     and not c.is_pitcher
                     and not (c.game_id == anchor_game and c.team.upper() != anchor_team)
                     and current_counts.get(c.team, 0)
                         < _team_batter_cap(c.team, stack_eligible_teams)
                     and (c.game_id is None
                          or current_game_counts.get(c.game_id, 0)
                              < MAX_PLAYERS_PER_GAME_BATTERS)),
                    None,
                )
                if replacement:
                    removed_name = lineup[idx].player_name
                    lineup_names.discard(removed_name)
                    lineup[idx] = replacement
                    lineup_names.add(replacement.player_name)
                    logger.info(
                        "Batter-team cap (%d): replaced %s (%s) with %s (%s)",
                        team_cap, removed_name, team,
                        replacement.player_name, replacement.team,
                    )

    # Rule 3: Per-game batter cap — at most 2 batters from any one game.
    # Catches the mixed-side case: 2 batters from team A + 2 from team B
    # in the same game would be 4-per-game despite each team being capped at 2.
    game_counts = _batter_game_counts(lineup)
    for game_id, count in game_counts.items():
        if count > MAX_PLAYERS_PER_GAME_BATTERS:
            game_indices = sorted(
                [i for i, c in enumerate(lineup)
                 if not _protected(i) and c.game_id == game_id],
                key=lambda i: lineup[i].filter_ev,
                reverse=True,
            )
            lineup_names = {c.player_name for c in lineup}
            for idx in game_indices[MAX_PLAYERS_PER_GAME_BATTERS:]:
                current_team_counts = _batter_team_counts(lineup)
                current_game_counts = _batter_game_counts(lineup)
                replacement = next(
                    (c for c in all_candidates_sorted
                     if c.player_name not in lineup_names
                     and not c.is_pitcher
                     and not (c.game_id == anchor_game and c.team.upper() != anchor_team)
                     and current_team_counts.get(c.team, 0)
                         < _team_batter_cap(c.team, stack_eligible_teams)
                     and (c.game_id is None
                          or current_game_counts.get(c.game_id, 0)
                              < MAX_PLAYERS_PER_GAME_BATTERS)),
                    None,
                )
                if replacement:
                    removed_name = lineup[idx].player_name
                    lineup_names.discard(removed_name)
                    lineup[idx] = replacement
                    lineup_names.add(replacement.player_name)
                    logger.info(
                        "Per-game batter cap (%d): replaced %s (game=%s) with %s (game=%s)",
                        MAX_PLAYERS_PER_GAME_BATTERS, removed_name, game_id,
                        replacement.player_name, replacement.game_id,
                    )

    # Rule 4 (V10.5): At most 1 pitcher.  0 (pure-batter shootout shape) and
    # 1 (anchored) are both legal; the EV-driven chooser in `_enforce_composition`
    # picks between them.  Anything > 1 is a structural bug (we never assemble
    # multiple pitchers into the same lineup), so warn loudly if it happens.
    pitcher_count_final = sum(1 for c in lineup if c.is_pitcher)
    if pitcher_count_final > REQUIRED_PITCHERS_IN_LINEUP:
        logger.warning(
            "Pitcher-count invariant violated: expected at most %d, got %d in %s",
            REQUIRED_PITCHERS_IN_LINEUP, pitcher_count_final,
            [c.player_name for c in lineup],
        )

    return lineup


def _apply_game_diversification(
    lineup: list[FilteredCandidate],
) -> list[str]:
    """Report lineup game/team spread for diagnostics.

    V10.0: stacking is the strategy, so there is no per-game cap on teammates;
    the only structural guard is "no opposing batter in the anchor's game",
    enforced in _enforce_composition and _validate_lineup_structure.
    """
    warnings: list[str] = []
    if not lineup:
        return warnings

    game_counts: dict[str | int | None, int] = {}
    for c in lineup:
        gid = c.game_id
        if gid is not None:
            game_counts[gid] = game_counts.get(gid, 0) + 1

    games_represented = len(game_counts) if game_counts else 0

    from collections import Counter
    batter_team_counts = Counter(c.team for c in lineup if not c.is_pitcher)
    stack_teams = [
        f"{t}x{n}" for t, n in batter_team_counts.items() if n >= 2
    ]
    logger.info(
        "Lineup spread: %d games, batter stacks: %s",
        games_represented, ", ".join(stack_teams) if stack_teams else "none",
    )

    return warnings


def _smart_slot_assignment(
    candidates: list[FilteredCandidate],
) -> list[FilterSlotAssignment]:
    """Slot assignment: pitcher (if present) anchors Slot 1, then highest-EV
    batters fill remaining slots in descending order.

    V10.5: extended to handle the 0-pitcher pure-batter case.  When the lineup
    has no pitcher, the highest-EV batter gets Slot 1 (2.0×) and the remaining
    four batters fill Slots 2-5 by filter_ev descending.
    """
    if not candidates:
        return []

    slot_mults = dict(SLOT_MULTIPLIERS)  # {1: 2.0, 2: 1.8, ...}

    pitcher = next((c for c in candidates if c.is_pitcher), None)
    batters = sorted(
        [c for c in candidates if not c.is_pitcher],
        key=lambda c: c.filter_ev,
        reverse=True,
    )

    assignments: list[FilterSlotAssignment] = []
    slots_desc = sorted(slot_mults.items(), key=lambda x: x[1], reverse=True)

    if pitcher is not None:
        # Pitcher → Slot 1 (2.0×); batters → Slots 2-5 by EV descending.
        anchor_mult = slot_mults[PITCHER_ANCHOR_SLOT]
        slot_value = pitcher.filter_ev * (anchor_mult / BASE_MULTIPLIER)
        assignments.append(FilterSlotAssignment(
            slot_index=PITCHER_ANCHOR_SLOT,
            slot_mult=anchor_mult,
            candidate=pitcher,
            expected_slot_value=round(slot_value, 2),
        ))
        remaining_slots = [(idx, mult) for idx, mult in slots_desc if idx != PITCHER_ANCHOR_SLOT]
        for player, (slot_idx, slot_mult) in zip(batters, remaining_slots):
            slot_value = player.filter_ev * (slot_mult / BASE_MULTIPLIER)
            assignments.append(FilterSlotAssignment(
                slot_index=slot_idx,
                slot_mult=slot_mult,
                candidate=player,
                expected_slot_value=round(slot_value, 2),
            ))
    else:
        # 0-pitcher lineup: top batter takes Slot 1, rest descend.
        for player, (slot_idx, slot_mult) in zip(batters, slots_desc):
            slot_value = player.filter_ev * (slot_mult / BASE_MULTIPLIER)
            assignments.append(FilterSlotAssignment(
                slot_index=slot_idx,
                slot_mult=slot_mult,
                candidate=player,
                expected_slot_value=round(slot_value, 2),
            ))

    assignments.sort(key=lambda a: a.slot_index)
    return assignments


# ---------------------------------------------------------------------------
# Main filter pipeline
# ---------------------------------------------------------------------------

def _exclude_fade_players(candidates: list[FilteredCandidate]) -> list[FilteredCandidate]:
    """Popularity gate (V10.5): exclude FADE batters; keep FADE pitchers
    with a soft EV penalty applied later in `_compute_base_ev`.

    Rationale: the crowd is structurally wrong about batter ownership
    (TARGET batters average RS 3.57 / HV 73% vs FADE batters RS 0.98 /
    HV 9.6%), so removing FADE batters is the highest-EV pre-EV filter
    we have.  But pitcher outcomes are one-player-dependent, so the
    FADE-vs-TARGET differential collapses to ~1.4×; eliminating
    confirmed probable starters of heavy ML favorites (Ohtani, Yamamoto,
    Fried) on popularity alone systematically misses obvious value.

    Pitchers therefore stay in the pool here and pay PITCHER_FADE_PENALTY
    (0.85×) in EV — a soft "name-recognition tax" that lets genuinely
    strong matchups survive.

    No fallback path: if every pitcher is excluded somewhere upstream
    we still fail fast, because a 0-pitcher pool means the candidate
    resolver itself is broken.
    """
    filtered = [
        c for c in candidates
        if c.is_pitcher or c.popularity != PopularityClass.FADE
    ]
    excluded = len(candidates) - len(filtered)
    fade_pitcher_count = sum(
        1 for c in filtered
        if c.is_pitcher and c.popularity == PopularityClass.FADE
    )
    if excluded or fade_pitcher_count:
        logger.info(
            "Popularity gate: excluded %d FADE batters; kept %d FADE pitchers "
            "with soft penalty (%d pool size)",
            excluded, fade_pitcher_count, len(filtered),
        )
    if not any(c.is_pitcher for c in filtered):
        raise ValueError(
            "Candidate pool contains no pitchers. "
            "Cannot build a lineup without an SP anchor."
        )
    return filtered


def run_filter_strategy(
    candidates: list[FilteredCandidate],
    slate_classification: SlateClassification,
    skip_fade_gate: bool = False,
) -> FilterOptimizedLineup:
    """
    Run the full "Filter, Not Forecast" pipeline.

    This is the main entry point. Takes pre-scored, pre-filtered
    candidates and produces an optimized lineup following all 5 filters.

    Steps:
    1. Exclude FADE players (popularity gate) — unless skip_fade_gate=True
    2. Compute filter-adjusted EV for each candidate (env + trait + context)
    3. Enforce composition (pitcher anchor + 4 batters)
    4. Check game diversification
    5. Smart slot assignment

    The `skip_fade_gate` flag exists so `run_dual_filter_strategy` can filter
    FADE once up-front and then re-use the filtered pool for both Starting 5
    and Moonshot, avoiding a redundant exclusion pass (and duplicate log lines).
    """
    if not candidates:
        return FilterOptimizedLineup(
            slots=[],
            total_expected_value=0.0,
            strategy="filter_not_forecast",
            slate_classification=slate_classification,
        )

    # Step 1: Popularity gate — remove FADE players before EV computation.
    if not skip_fade_gate:
        candidates = _exclude_fade_players(candidates)

    # Mark blowout-game players before EV computation so _compute_filter_ev()
    # can apply the stack_bonus without needing slate_classification as a parameter.
    # STACK_BONUS (1.20× EV) is gated to PATH 1 blowout favorites only.
    # PATH 2 shootout sides become stack-eligible (mini-stack cap lifts to 2)
    # but do NOT receive the bonus — they're in a high-run game, not a
    # predictable blowout, so the asymmetric upside is smaller.
    blowout_teams = {
        g.favored_team.upper()
        for g in slate_classification.stackable_games
        if g.favored_team and g.is_blowout_favorite
    }
    for c in candidates:
        c.is_in_blowout_game = c.team.upper() in blowout_teams

    # Step 2: Compute filter-adjusted EV (env + trait + context)
    for c in candidates:
        c.filter_ev = _compute_filter_ev(c)

    # Step 3: Enforce composition — pitcher anchor + 4 batters by EV rank.
    lineup = _enforce_composition(candidates, slate_classification)

    # Step 4: Game diversification (safety-net; team/game caps enforced upstream)
    warnings = _apply_game_diversification(lineup)

    # Step 5: Smart slot assignment
    slots = _smart_slot_assignment(lineup)

    total_ev = sum(s.expected_slot_value for s in slots)
    pitcher_count = sum(1 for s in slots if s.candidate.is_pitcher)
    hitter_count = len(slots) - pitcher_count

    return FilterOptimizedLineup(
        slots=slots,
        total_expected_value=round(total_ev, 2),
        strategy="filter_not_forecast",
        slate_classification=slate_classification,
        composition={"pitchers": pitcher_count, "hitters": hitter_count},
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Moonshot — completely different 5, anti-crowd, sharp-signal, explosive
# ---------------------------------------------------------------------------

def _compute_moonshot_filter_ev(candidate: FilteredCandidate) -> float:
    """Compute Moonshot EV: base env×trait + sharp signal + explosive upside.

    V9.0: Same base formula as Starting 5 (env × trait × context).
    Moonshot differentiates via two bonuses applied on top:
      - sharp_bonus     (+35% max) — underground Reddit/FanGraphs analyst buzz
      - explosive_bonus (+20% max) — power_profile (batters) or k_rate (pitchers)

    Players with underground buzz or boom-or-bust upside rank higher here
    than in Starting 5, naturally diverging the two lineups without forcing
    player exclusion.
    """
    base_ev = _compute_base_ev(candidate)

    sharp_bonus = 1.0 + (candidate.sharp_score / 100.0) * MOONSHOT_SHARP_BONUS_MAX
    explosive_trait = get_trait_score(
        candidate.traits, "k_rate" if candidate.is_pitcher else "power_profile"
    )
    explosive_bonus = 1.0 + (explosive_trait / 25.0) * MOONSHOT_EXPLOSIVE_BONUS_MAX

    return base_ev * sharp_bonus * explosive_bonus


@dataclass
class DualFilterOptimizedResult:
    starting_5: FilterOptimizedLineup
    moonshot: FilterOptimizedLineup


def _build_best_variant(
    pitcher: FilteredCandidate | None,
    batter_pool: list[FilteredCandidate],
    slate_class: SlateClassification,
    stack_eligible_teams: set[str],
) -> list[FilteredCandidate]:
    """Pick the higher-EV of (1P+4B with given pitcher) vs (0P+5B from pool).

    Used by both Starting 5 and Moonshot to honour the V10.5 rule that
    composition is EV-driven — pure-batter lineups win on shootout slates,
    pitcher-anchored lineups win when the SP's slot-1 multiplier overcomes
    the marginal batter.

    `batter_pool` must already be sorted by the caller's relevant filter_ev
    (base EV for Starting 5; moonshot EV for Moonshot).  Pitchers are pruned
    from `batter_pool` before this function is called.
    """
    anchor_game_id = pitcher.game_id if pitcher is not None else None
    anchor_team = pitcher.team.upper() if pitcher is not None else ""

    # Variant A: 1P + 4B
    anchor_lineup: list[FilteredCandidate] = []
    if pitcher is not None:
        anchor_lineup = [pitcher]
        anchor_lineup.extend(_fill_batter_slots(
            batter_pool, anchor_game_id, anchor_team, stack_eligible_teams,
        ))
        anchor_lineup = _validate_lineup_structure(
            anchor_lineup, batter_pool, anchor_pitcher=pitcher,
            stack_eligible_teams=stack_eligible_teams,
        )

    # Variant B: 0P + 5B (no anchor restriction — top-5 batters)
    pure_batters = _fill_batter_slots(
        batter_pool, anchor_game_id=None, anchor_team="",
        stack_eligible_teams=stack_eligible_teams, slots_to_fill=5,
    )
    pure_lineup: list[FilteredCandidate] = []
    if len(pure_batters) == 5:
        pure_lineup = _validate_lineup_structure(
            pure_batters, batter_pool, anchor_pitcher=None,
            stack_eligible_teams=stack_eligible_teams,
        )

    anchor_ev = _lineup_total_ev(anchor_lineup) if len(anchor_lineup) == 5 else -1.0
    pure_ev = _lineup_total_ev(pure_lineup) if len(pure_lineup) == 5 else -1.0
    return anchor_lineup if anchor_ev >= pure_ev else pure_lineup


def run_dual_filter_strategy(
    candidates: list[FilteredCandidate],
    slate_classification: SlateClassification,
) -> DualFilterOptimizedResult:
    """Produce Starting 5 and Moonshot from the same FADE-batter-excluded pool.

    V10.5: each lineup independently chooses its optimal shape (1P+4B or
    0P+5B) based on slot-weighted EV.  When both choose 1P, they share the
    same pitcher anchor (highest base EV SP) — the existing divergence
    pattern (different batters via sharp×explosive re-ranking) is preserved.
    When the slate is a shootout and the top batter EVs dominate, either
    or both lineups may go pure-batter.  Zero batter overlap is guaranteed
    by construction.
    """
    # 1. Popularity gate (V10.5: bifurcated — FADE batters out, FADE pitchers
    #    stay with soft EV penalty applied in _compute_base_ev).
    candidates = _exclude_fade_players(candidates)

    # 2. Mark blowout-game players and compute base EV for all candidates.
    # STACK_BONUS (1.20× EV) is gated to PATH 1 blowout favorites only.
    blowout_teams = {
        g.favored_team.upper()
        for g in slate_classification.stackable_games
        if g.favored_team and g.is_blowout_favorite
    }
    for c in candidates:
        c.is_in_blowout_game = c.team.upper() in blowout_teams
        c.filter_ev = _compute_filter_ev(c)

    # 3. Identify the shared pitcher anchor (highest base EV).  May be None
    #    in degenerate slates with no pitcher; the variant builder handles that.
    sorted_by_base = sorted(candidates, key=lambda c: c.filter_ev, reverse=True)
    shared_pitcher = next((c for c in sorted_by_base if c.is_pitcher), None)
    if shared_pitcher is None:
        raise ValueError("Candidate pool contains no pitcher after FADE exclusion.")

    stack_eligible_teams = _compute_stack_eligible_teams(slate_classification)

    logger.info(
        "Dual strategy: shared anchor candidate=%s (EV=%.2f) stack_eligible=%s",
        shared_pitcher.player_name, shared_pitcher.filter_ev,
        sorted(stack_eligible_teams) or "none",
    )

    # 4. Base batter pool: no pitchers, exclude opposing batters in the
    #    candidate anchor's game (they'd be invalid in the anchor variant
    #    and we'd rather keep one pool consistent across variants).
    anchor_game_id = shared_pitcher.game_id
    anchor_team = shared_pitcher.team.upper()
    base_batter_pool = [
        c for c in sorted_by_base
        if not c.is_pitcher
        and not (
            anchor_game_id is not None
            and c.game_id == anchor_game_id
            and c.team.upper() != anchor_team
        )
    ]

    # 5. Starting 5: pick best of 1P+4B vs 0P+5B by base EV.
    s5_lineup = _build_best_variant(
        shared_pitcher, base_batter_pool, slate_classification, stack_eligible_teams,
    )
    s5_warnings = _apply_game_diversification(s5_lineup)
    s5_slots = _smart_slot_assignment(s5_lineup)
    s5_total_ev = sum(sl.expected_slot_value for sl in s5_slots)
    s5_pitcher_count = sum(1 for c in s5_lineup if c.is_pitcher)

    starting_5 = FilterOptimizedLineup(
        slots=s5_slots,
        total_expected_value=round(s5_total_ev, 2),
        strategy="filter_not_forecast",
        slate_classification=slate_classification,
        composition={"pitchers": s5_pitcher_count, "hitters": len(s5_lineup) - s5_pitcher_count},
        warnings=s5_warnings,
    )

    # 6. Moonshot: re-rank remaining batters by sharp×explosive EV, then pick
    #    best of 1P+4B vs 0P+5B.  Non-overlap with Starting 5 batters is
    #    enforced by removing s5 batter keys from the moonshot batter pool.
    s5_batter_keys = {(c.player_name, c.team) for c in s5_lineup if not c.is_pitcher}
    moonshot_pool = [
        c for c in base_batter_pool
        if (c.player_name, c.team) not in s5_batter_keys
    ]
    if len(moonshot_pool) < 4:
        raise ValueError(
            f"Insufficient non-overlapping batters for Moonshot: "
            f"{len(moonshot_pool)} available after Starting 5 selection."
        )
    for c in moonshot_pool:
        c.filter_ev = _compute_moonshot_filter_ev(c)
    moonshot_pool.sort(key=lambda c: c.filter_ev, reverse=True)

    moonshot_lineup = _build_best_variant(
        shared_pitcher, moonshot_pool, slate_classification, stack_eligible_teams,
    )
    moonshot_warnings = _apply_game_diversification(moonshot_lineup)
    moonshot_slots = _smart_slot_assignment(moonshot_lineup)
    moonshot_total_ev = sum(sl.expected_slot_value for sl in moonshot_slots)
    moonshot_pitcher_count = sum(1 for c in moonshot_lineup if c.is_pitcher)

    moonshot = FilterOptimizedLineup(
        slots=moonshot_slots,
        total_expected_value=round(moonshot_total_ev, 2),
        strategy="moonshot",
        slate_classification=slate_classification,
        composition={"pitchers": moonshot_pitcher_count, "hitters": len(moonshot_lineup) - moonshot_pitcher_count},
        warnings=moonshot_warnings,
    )

    return DualFilterOptimizedResult(starting_5=starting_5, moonshot=moonshot)
