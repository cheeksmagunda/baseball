"""
Filter Strategy V9.0: "Filter, Not Forecast" — the five-filter pipeline.

This is the core strategic engine from the Master Strategy Document.
We do NOT predict RS. We identify conditions under which high RS is
most likely to emerge, then select from that filtered pool.

Five filters applied sequentially:
  1. Slate Architecture    — classify the day type (informational only)
  2. Popularity gate       — FADE players (high pre-game media attention) are
                             excluded from the candidate pool.  TARGET/NEUTRAL
                             pass with no bonus.  No RS data involved.
  3. Environmental Advantage — PRIMARY signal: game conditions (Vegas O/U,
                             opposing ERA, bullpen ERA, park, weather, platoon,
                             batting order, moneyline). Groups A/B/C/D.
  4. Individual Explosive Traits — SECONDARY: K/9, ISO, barrel%, speed, form.
  5. Slot Sequencing (Pitcher-Anchor) — 1 SP pinned to Slot 1 (2.0×);
                             4 batters fill Slots 2–5 by filter_ev.

EV formula (V9.0):
    base_ev = env_factor × trait_factor × stack_bonus × dnp_adj × 100

    Starting 5:  base_ev (pure env + trait ranking)
    Moonshot:    base_ev × sharp_bonus × explosive_bonus
                 sharp_bonus    — underground Reddit/FanGraphs analyst buzz (+35% max)
                 explosive_bonus — power_profile / k_rate upside (+20% max)

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
    MIN_SCORE_THRESHOLD,
    PITCHER_ENV_WEAK_OPP_OPS,
    PITCHER_ENV_MIN_K_PER_9,
    MOONSHOT_SHARP_BONUS_MAX,
    MOONSHOT_EXPLOSIVE_BONUS_MAX,
    MOONSHOT_SAME_TEAM_PENALTY,
    REQUIRED_PITCHERS_IN_LINEUP,
    PITCHER_ANCHOR_SLOT,
    MAX_PLAYERS_PER_TEAM,
    MAX_PLAYERS_PER_GAME,
    MAX_OPPONENTS_SAME_GAME,
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
    BATTER_ENV_GROUP_A_SOFT_CAP_POINT,
    BATTER_ENV_GROUP_A_SOFT_CAP_SLOPE,
    BATTER_ENV_PARK_HITTER_FRIENDLY,
    BATTER_ENV_PARK_NEUTRAL,
    BATTER_ENV_WIND_SPEED_MIN,
    BATTER_ENV_WARM_TEMP_THRESHOLD,
    BATTER_ENV_WARM_TEMP_BONUS,
    BATTER_ENV_WIND_OUT_BONUS,
    BATTER_ENV_WIND_OUT_DIRECTIONS,
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
    """A game identified as a blowout/stacking candidate."""
    game_id: int | str | None = None
    favored_team: str = ""
    moneyline: int | None = None
    vegas_total: float | None = None
    opp_starter_era: float | None = None


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

        # Blowout detection (§2 Pillar 2): moneyline ≥ -200 = projected blowout
        home_ml = g.get("home_moneyline")
        away_ml = g.get("away_moneyline")
        if home_ml is not None and home_ml <= BLOWOUT_MONEYLINE_THRESHOLD:
            blowout_games += 1
            stackable.append(StackableGame(
                game_id=g.get("game_id"),
                favored_team=g.get("home_team", ""),
                moneyline=home_ml,
                vegas_total=vt,
                opp_starter_era=g.get("away_starter_era"),
            ))
        elif away_ml is not None and away_ml <= BLOWOUT_MONEYLINE_THRESHOLD:
            blowout_games += 1
            stackable.append(StackableGame(
                game_id=g.get("game_id"),
                favored_team=g.get("away_team", ""),
                moneyline=away_ml,
                vegas_total=vt,
                opp_starter_era=g.get("home_starter_era"),
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
    CRITICAL RULE: card_boost and drafts are DISPLAY-ONLY FIELDS.
    They must NEVER be used in EV computations, optimization, or scoring.
    Both are only revealed during/after the draft and cannot be pre-game inputs.
    ========================================================================

    EV is computed from pre-game signals only:
      env_score   — Vegas O/U, opposing ERA/bullpen, park, weather, platoon, batting order
      total_score — season-level trait quality (K/9, ISO, barrel%, speed, recent form)
      popularity  — media attention (Google Trends, ESPN, Reddit) — NOT platform ownership

    Fields marked "display only":
      card_boost  — user-facing boost multiplier (0 to +3.0x), revealed during draft
      drafts      — user-facing platform ownership count, revealed during draft

    These fields exist solely for user communication. They MUST NOT leak into
    EV computations. Leaking in-draft dynamic signals corrupts the entire model.

    See CLAUDE.md § "Signal Isolation: ABSOLUTE RULE" for full rationale.
    """
    player_name: str
    team: str
    position: str
    card_boost: float     # display only — revealed during draft, not a predictive input
    total_score: float    # 0-100 from scoring engine (pre-game season stats)
    env_score: float      # 0-1.0 from environmental filter (pre-game conditions)
    env_factors: list[str] = field(default_factory=list)
    env_unknown_count: int = 0  # how many env factors were missing data
    popularity: PopularityClass = PopularityClass.NEUTRAL  # web-scraped (pre-game)
    game_id: int | str | None = None  # for diversification tracking
    is_pitcher: bool = False
    sharp_score: float = 0.0
    drafts: int | None = None  # display only — revealed during draft
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

    return (
        env_factor
        * volatility_amplifier
        * trait_factor
        * stack_bonus
        * dnp_adj
        * 100.0
    )


def _compute_filter_ev(candidate: FilteredCandidate) -> float:
    """Compute Starting 5 EV: env × trait × context (no RS data)."""
    return _compute_base_ev(candidate)


def _enforce_composition(
    candidates: list[FilteredCandidate],
    slate_class: SlateClassification,
) -> list[FilteredCandidate]:
    """
    V6.0 pitcher-anchored composition: 1 pitcher + 4 batters.

    Retains V5.0's 1P+4B shape.  The popularity-first EV formula determines
    WHICH pitcher and WHICH batters fill those slots — but the composition
    is fixed at 1 SP (Slot 1) + 4 batters (Slots 2-5).

    Construction:
    1. Sort all candidates by filter_ev descending.
    2. Pick the highest-EV pitcher as anchor.  Its game_id is blocked.
    3. Fill 4 batter slots by filter_ev, respecting MAX_PLAYERS_PER_TEAM
       and MAX_PLAYERS_PER_GAME.
    """
    all_sorted = sorted(candidates, key=lambda c: c.filter_ev, reverse=True)

    # Phase 1: Select the anchor pitcher (highest-EV pitcher in the pool).
    anchor_pitcher = next((c for c in all_sorted if c.is_pitcher), None)
    if anchor_pitcher is None:
        raise ValueError(
            "Candidate pool contains no pitcher. "
            "Cannot build a lineup without an SP anchor."
        )

    blocked_game_id = anchor_pitcher.game_id

    # Phase 2: Batters ordered by filter_ev (popularity-first ranking).
    ordered_batters = [c for c in all_sorted if not c.is_pitcher]

    # Phase 3: Fill 4 batter slots.
    lineup: list[FilteredCandidate] = [anchor_pitcher]
    anchor_team = anchor_pitcher.team.upper()
    team_used: dict[str, int] = {anchor_team: 1}
    game_team_used: dict[tuple, int] = {}
    game_teams: dict[int | str | None, set[str]] = {}
    if blocked_game_id is not None:
        game_team_used[(blocked_game_id, anchor_team)] = 1
        game_teams[blocked_game_id] = {anchor_team}

    for c in ordered_batters:
        if len(lineup) == 5:
            break

        # Block the anchor pitcher's game (no negative correlation).
        if blocked_game_id is not None and c.game_id == blocked_game_id:
            continue

        team_key = c.team.upper()
        if team_used.get(team_key, 0) >= MAX_PLAYERS_PER_TEAM:
            continue

        if c.game_id is not None:
            gt_key = (c.game_id, team_key)
            if game_team_used.get(gt_key, 0) >= MAX_PLAYERS_PER_GAME:
                continue
            existing_teams = game_teams.get(c.game_id, set())
            opponent_teams = existing_teams - {team_key}
            if opponent_teams:
                opp_count = sum(
                    game_team_used.get((c.game_id, t), 0)
                    for t in opponent_teams
                )
                if opp_count >= MAX_OPPONENTS_SAME_GAME:
                    if team_key not in existing_teams:
                        continue
            game_team_used[gt_key] = game_team_used.get(gt_key, 0) + 1
            game_teams.setdefault(c.game_id, set()).add(team_key)

        team_used[team_key] = team_used.get(team_key, 0) + 1
        lineup.append(c)

    # Validate team/game caps.
    lineup = _validate_lineup_structure(
        lineup, ordered_batters, anchor_pitcher=anchor_pitcher
    )
    pitcher_count = sum(1 for c in lineup if c.is_pitcher)
    logger.info(
        "V6.0 composition: %dP/%dH — anchor=%s (EV=%.2f) (candidates: %d)",
        pitcher_count, len(lineup) - pitcher_count,
        anchor_pitcher.player_name, anchor_pitcher.filter_ev,
        len(candidates),
    )
    return lineup


def _validate_lineup_structure(
    lineup: list[FilteredCandidate],
    all_candidates_sorted: list[FilteredCandidate],
    anchor_pitcher: FilteredCandidate | None = None,
) -> list[FilteredCandidate]:
    """
    V6.0 validation — enforce team/game caps and 1-pitcher invariant.

    The anchor pitcher is protected from all swaps.  Replacement candidates
    are batters only (to preserve 1P+4B).
    """
    if len(lineup) < 5:
        return lineup

    anchor_idx: int | None = None
    if anchor_pitcher is not None:
        for i, c in enumerate(lineup):
            if c.player_name == anchor_pitcher.player_name:
                anchor_idx = i
                break

    def _protected(idx: int) -> bool:
        return anchor_idx is not None and idx == anchor_idx

    # Rule 1: Max N players per team — anchor exempt.
    from collections import Counter
    team_counts = Counter(c.team for c in lineup)
    for team, count in team_counts.items():
        if count > MAX_PLAYERS_PER_TEAM:
            team_indices = sorted(
                [i for i, c in enumerate(lineup)
                 if c.team == team and not _protected(i)],
                key=lambda i: lineup[i].filter_ev,
                reverse=True,
            )
            lineup_names = {c.player_name for c in lineup}
            anchor_game = anchor_pitcher.game_id if anchor_pitcher is not None else None
            for idx in team_indices[MAX_PLAYERS_PER_TEAM:]:
                current_team_counts = Counter(c.team for c in lineup)
                replacement = next(
                    (c for c in all_candidates_sorted
                     if c.player_name not in lineup_names
                     and not c.is_pitcher
                     and c.game_id != anchor_game
                     and current_team_counts.get(c.team, 0) < MAX_PLAYERS_PER_TEAM),
                    None,
                )
                if replacement:
                    removed_name = lineup[idx].player_name
                    lineup_names.discard(removed_name)
                    lineup[idx] = replacement
                    lineup_names.add(replacement.player_name)
                    logger.info(
                        "Team cap: replaced %s (%s) with %s (%s)",
                        removed_name, team,
                        replacement.player_name, replacement.team,
                    )

    # Rule 2: Max N players per game — anchor exempt.
    game_counts = Counter(c.game_id for c in lineup if c.game_id is not None)
    for game_id, count in game_counts.items():
        if count > MAX_PLAYERS_PER_GAME:
            game_indices = sorted(
                [i for i, c in enumerate(lineup)
                 if c.game_id == game_id and not _protected(i)],
                key=lambda i: lineup[i].filter_ev,
                reverse=True,
            )
            lineup_names = {c.player_name for c in lineup}
            anchor_game = anchor_pitcher.game_id if anchor_pitcher is not None else None
            for idx in game_indices[MAX_PLAYERS_PER_GAME:]:
                current_game_counts = Counter(
                    c.game_id for c in lineup if c.game_id is not None
                )
                replacement = next(
                    (c for c in all_candidates_sorted
                     if c.player_name not in lineup_names
                     and not c.is_pitcher
                     and c.game_id != anchor_game
                     and (c.game_id is None
                          or current_game_counts.get(c.game_id, 0) < MAX_PLAYERS_PER_GAME)),
                    None,
                )
                if replacement:
                    removed_name = lineup[idx].player_name
                    lineup_names.discard(removed_name)
                    lineup[idx] = replacement
                    lineup_names.add(replacement.player_name)
                    logger.info(
                        "Game cap: replaced %s (game=%s) with %s (game=%s)",
                        removed_name, game_id,
                        replacement.player_name, replacement.game_id,
                    )

    # Rule 3: Exactly 1 pitcher (sanity check).
    pitcher_count_final = sum(1 for c in lineup if c.is_pitcher)
    if pitcher_count_final != REQUIRED_PITCHERS_IN_LINEUP:
        logger.warning(
            "Pitcher-count invariant violated: expected %d, got %d in %s",
            REQUIRED_PITCHERS_IN_LINEUP, pitcher_count_final,
            [c.player_name for c in lineup],
        )

    return lineup


def _apply_game_diversification(
    lineup: list[FilteredCandidate],
) -> list[str]:
    """
    Check game diversification.

    Max 1 player per game is enforced during composition and validation.
    This function now serves as a safety-net warning if any violations leaked
    through, plus reports game spread for diagnostics.
    """
    warnings = []
    if not lineup:
        return warnings

    game_counts: dict[str | int | None, int] = {}
    for c in lineup:
        gid = c.game_id
        if gid is not None:
            game_counts[gid] = game_counts.get(gid, 0) + 1

    games_represented = len(game_counts) if game_counts else 0

    # Invariant check: _enforce_composition + _validate_lineup_structure guarantee
    # max 1 player per game.  If that guarantee is ever broken, raise loudly
    # rather than papering over the violation with a silent EV penalty.
    for gid, count in game_counts.items():
        if count > MAX_PLAYERS_PER_GAME:
            violators = [c.player_name for c in lineup if c.game_id == gid]
            raise ValueError(
                f"Game-cap invariant violated: game {gid} has {count} players "
                f"(cap={MAX_PLAYERS_PER_GAME}): {violators}. "
                "This is a bug in _enforce_composition or _validate_lineup_structure."
            )

    logger.info(
        "Game diversification: %d games represented across %d players",
        games_represented, len(lineup),
    )

    return warnings


def _smart_slot_assignment(
    candidates: list[FilteredCandidate],
) -> list[FilterSlotAssignment]:
    """V6.0 slot assignment: pitcher anchors Slot 1, batters fill Slots 2-5.

    Retains V5.0 structure.  The pitcher is pinned to Slot 1 (2.0x).
    Batters are assigned to Slots 2-5 by filter_ev descending (highest
    EV batter gets Slot 2 at 1.8x).
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

    # Pitcher → Slot 1 (2.0x multiplier).
    if pitcher is not None:
        anchor_mult = slot_mults[PITCHER_ANCHOR_SLOT]
        slot_value = pitcher.filter_ev * (anchor_mult / BASE_MULTIPLIER)
        assignments.append(FilterSlotAssignment(
            slot_index=PITCHER_ANCHOR_SLOT,
            slot_mult=anchor_mult,
            candidate=pitcher,
            expected_slot_value=round(slot_value, 2),
        ))

    # Batters → remaining slots by filter_ev descending.
    batter_slots = sorted(
        ((idx, mult) for idx, mult in slot_mults.items() if idx != PITCHER_ANCHOR_SLOT),
        key=lambda x: x[1],
        reverse=True,
    )

    for player, (slot_idx, slot_mult) in zip(batters, batter_slots):
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
    """Popularity gate: remove FADE-classified players before EV computation.

    FADE = high pre-game media attention (Google Trends, ESPN, Reddit).
    These players are excluded from the candidate pool entirely — not penalised
    via EV, not ranked last, just removed.  TARGET and NEUTRAL pass with no bonus.

    Fails fast if the gate leaves no pitchers.  No fallback: a pool that cannot
    supply an SP anchor is a data/classification problem upstream, not something
    the optimizer should paper over.
    """
    filtered = [c for c in candidates if c.popularity != PopularityClass.FADE]
    excluded = len(candidates) - len(filtered)
    if excluded:
        logger.info(
            "Popularity gate: excluded %d FADE players (%d remain)",
            excluded, len(filtered),
        )
    if not any(c.is_pitcher for c in filtered):
        raise ValueError(
            "Candidate pool contains no non-FADE pitchers. "
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
    blowout_teams = {
        g.favored_team.upper()
        for g in slate_classification.stackable_games
        if g.favored_team
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


def run_dual_filter_strategy(
    candidates: list[FilteredCandidate],
    slate_classification: SlateClassification,
) -> DualFilterOptimizedResult:
    """Produce both Starting 5 and Moonshot from the same candidate pool.

    Starting 5: rank by env + trait EV; pitcher anchors Slot 1.
    Moonshot:   same base EV + sharp signal bonus + explosive trait bonus.
                Picks from the same FADE-excluded pool as Starting 5.
                Player overlap with Starting 5 is allowed — the two lineups
                diverge naturally via different EV formulas, not forced exclusion.
                Same-team penalty (0.85×) provides soft portfolio diversification.

    The FADE exclusion gate runs once here, before either lineup is built.
    """
    # Apply popularity gate once — both lineups draw from this filtered pool.
    candidates = _exclude_fade_players(candidates)

    # Phase 1: Starting 5 — pass skip_fade_gate=True since we already filtered.
    starting_5 = run_filter_strategy(candidates, slate_classification, skip_fade_gate=True)

    s5_teams = {s.candidate.team.upper() for s in starting_5.slots}

    # Phase 2: Moonshot — same pool, different EV formula
    moonshot_pool = candidates  # no player exclusion

    for c in moonshot_pool:
        c.filter_ev = _compute_moonshot_filter_ev(c)
        if c.team.upper() in s5_teams:
            c.filter_ev *= MOONSHOT_SAME_TEAM_PENALTY

    moonshot_lineup = _enforce_composition(moonshot_pool, slate_classification)
    moonshot_warnings = _apply_game_diversification(moonshot_lineup)
    moonshot_slots = _smart_slot_assignment(moonshot_lineup)

    moonshot_total_ev = sum(s.expected_slot_value for s in moonshot_slots)
    moonshot_pitcher_count = sum(1 for s in moonshot_slots if s.candidate.is_pitcher)
    moonshot_hitter_count = len(moonshot_slots) - moonshot_pitcher_count

    moonshot = FilterOptimizedLineup(
        slots=moonshot_slots,
        total_expected_value=round(moonshot_total_ev, 2),
        strategy="moonshot",
        slate_classification=slate_classification,
        composition={"pitchers": moonshot_pitcher_count, "hitters": moonshot_hitter_count},
        warnings=moonshot_warnings,
    )

    return DualFilterOptimizedResult(starting_5=starting_5, moonshot=moonshot)
