"""
Filter Strategy V11.0: "Filter, Not Forecast" — popularity-agnostic, single lineup.

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

Single-lineup optimizer:
  run_filter_strategy(candidates, slate_class) -> FilterOptimizedLineup

Internal helpers:
  _compute_base_ev, _compute_filter_ev
  _enforce_composition, _validate_lineup_structure
  _smart_slot_assignment, _compute_dnp_adjustment, _compute_stack_eligible_teams
────────────────────────────────────────────────────────────────────────────

V11.0 (April 30): popularity removed — no more FADE/TARGET/NEUTRAL gating, no
sharp_score scraping, no Moonshot.  The optimizer ranks purely on env + trait
+ context.  Rationale: web-scraped popularity was a noisy proxy for "what the
crowd thinks", which actively distorted ranking toward contrarian picks even
when env + trait disagreed.  Pure pre-game signal ranking — predict high
performers, ignore the crowd.

Filters applied sequentially:
  1. Slate Architecture    — classify the day type + identify blowout/high-total
                             games that unlock stacking for their favored team.
  2. Environmental Advantage — PRIMARY signal: game conditions (Vegas O/U,
                             opposing ERA, opposing WHIP, opposing K/9,
                             bullpen ERA, park, weather, platoon, batting
                             order, moneyline, series context). Groups A/B/C/D.
  3. Individual Explosive Traits — SECONDARY: Statcast kinematics (FB velo/IVB/
                             extension/whiff/chase for SP; avg EV/hard-hit%/
                             barrel% for batters) + K/9 / HR/PA fallback.
  4. Slot Sequencing (Pitcher-Anchor + conditional stacks) — when the EV
                             chooser picks 1P+4B, 1 SP is pinned to Slot 1
                             (2.0×) and 4 batters fill Slots 2–5 honouring
                             per-team caps that vary by stack eligibility.
                             When the chooser picks 0P+5B, the highest-EV
                             batter takes Slot 1 instead.

EV formula (V11.0):
    base_ev = env_factor × volatility_amplifier × trait_factor
              × stack_bonus × dnp_adj × 100

    env_factor          — pitchers cap at PITCHER_ENV_MODIFIER_CEILING (1.20),
                          batters at ENV_MODIFIER_CEILING (1.30).
    volatility_amplifier— Env-CONDITIONAL: 1 + cv × MAX × (env − 0.5) × 2.
                          Boom-bust hitters amplified in good env, penalised
                          in bad env.
    trait_factor        — 0.85–1.15 (TRAIT_MODIFIER bounds).
    stack_bonus         — 1.20 if PATH 1 blowout favorite team, else 1.0.
    dnp_adj             — 0.70 (CONFIRMED_BAD) / 0.93 (UNKNOWN) / 1.0.

Stacking (two-path):
    A team contributes more than one batter ONLY if its game clears
    is_stack_eligible_game() — PATH 1 (ML ≤ -200 AND O/U ≥ 9.0, favored side
    only, earns STACK_BONUS) OR PATH 2 (O/U ≥ 10.5, both sides eligible, no
    bonus).  Every other team is capped at one batter per lineup.  See
    _compute_stack_eligible_teams and _team_batter_cap below.
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
    # V12 — only the wind direction sets are read from constants.py;
    # all other thresholds are inline in compute_*_env_score for clarity.
    BATTER_ENV_WIND_OUT_DIRECTIONS,
    BATTER_ENV_WIND_IN_DIRECTIONS,
    # Volatility amplifier
    BATTER_FORM_VOLATILITY_MAX,
    # Slate classification — quality-SP ERA gate
    QUALITY_SP_ERA_THRESHOLD,
    # V10.6 — asymmetric pitcher env ceiling (vs batter)
    PITCHER_ENV_MODIFIER_CEILING,
)
from app.core.utils import BASE_MULTIPLIER

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

def compute_pitcher_env_score(
    opp_team_ops: float | None = None,
    opp_team_k_pct: float | None = None,  # V12: ignored — audit shows this signal is dead (Q1=25% vs Q4=23% HV)
    pitcher_k_per_9: float | None = None,
    park_team: str | None = None,
    is_home: bool = False,
    team_moneyline: int | None = None,
    vegas_total: float | None = None,
    own_starter_era: float | None = None,
) -> tuple[float, list[str]]:
    """
    V12 pitcher env score (0-1.0) — calibrated against 35-slate / 222-pitcher
    historical audit.  Built ONLY from signals the data showed actually
    separate HV outcomes:

      1. Moneyline (PEAK at mild fav -120 to -180):
            heavy fav (≤-200) wins HV only 14.5%, mild fav wins HV 37.5%
            (across 33 slates).  This is THE pitcher predictor.
      2. Vegas O/U INVERSE (low total = pitcher game):
            Q1 (6.5-7.5) HV=31%, Q4 (8.5+) HV=18%
      3. Park HR factor (pitcher-friendly):
            Q1 (≤0.95) HV=35%, Q4 (≥1.04) HV=23%
      4. K/9 talent (mild bonus only — tail not separation):
            Q1 27% / Q4 29% HV — minor upside lever
      5. ERA tail (extreme floor + ceiling only — small effect):
            Q1 (≤2.5) RS=4.23, Q4 (≥5.3) RS=1.80 — solid for mean RS, but
            HV-rate is FLAT/inverted (talent in noisy ERA outperforms),
            so we apply a small trim only

    Signals deliberately REMOVED from V12 vs V11.0:
      - opp_team_k_pct (DEAD — Q1 25% vs Q4 23% HV, no separation)
      - opp_team_ops as primary (Q1 31% vs Q4 25% HV, weak monotonic, kept
        as small contribution rather than primary)
      - K/9 as primary (Q1 27% vs Q4 29% — basically flat on HV)
      - Heavy-favorite ML reward (was monotonic-positive; data shows PEAK
        not monotonic — heavy favs underperform, mild favs win)
      - Home-field flat +0.5 (no audit separation; ML / O/U / park already
        capture the meaningful asymmetry)
    """
    score = 0.0
    factors = []
    max_score = 4.0  # tuned so the strongest realistic combination saturates near 1.0

    # 1. Moneyline — PEAK at mild fav.  Most discriminating single signal.
    if team_moneyline is not None:
        ml = team_moneyline
        if -180 <= ml <= -120:
            score += 1.0
            factors.append(f"Mild favorite (ML={ml}) — peak pitcher win zone")
        elif -210 <= ml < -180 or -120 < ml <= -110:
            score += 0.7
            factors.append(f"Strong favorite (ML={ml})")
        elif ml < -210:
            score += 0.3
            factors.append(f"Heavy favorite (ML={ml}) — historical HV underperform")
        elif -109 <= ml <= 109:
            score += 0.4
            factors.append(f"Toss-up (ML={ml})")
        else:
            score += 0.2
            factors.append(f"Underdog (ML={ml})")

    # 2. Vegas O/U INVERSE — low totals = pitcher games
    if vegas_total is not None:
        if vegas_total <= 7.5:
            score += 1.0
            factors.append(f"Low-total pitcher game (O/U={vegas_total:.1f})")
        elif vegas_total <= 8.0:
            score += 0.8
        elif vegas_total <= 8.5:
            score += 0.5
        elif vegas_total <= 9.0:
            score += 0.2

    # 3. Park HR factor — pitcher-friendly
    if park_team:
        pf = PARK_HR_FACTORS.get(park_team, 1.0)
        if pf <= 0.95:
            score += 0.6
            factors.append(f"Pitcher park ({park_team}, factor={pf:.2f})")
        elif pf <= 1.0:
            score += 0.3
        # > 1.0 contributes 0 (don't penalise, just don't reward)

    # 4. K/9 talent — modest bonus
    if pitcher_k_per_9 is not None:
        if pitcher_k_per_9 >= 10.0:
            score += 0.4
            factors.append(f"Elite K/9 ({pitcher_k_per_9:.1f})")
        elif pitcher_k_per_9 >= 8.0:
            score += 0.2

    # 5. ERA tail — small lever for confirmed-elite vs confirmed-bust
    if own_starter_era is not None:
        if own_starter_era <= 3.0:
            score += 0.3
            factors.append(f"Elite ERA ({own_starter_era:.2f})")
        elif own_starter_era >= 6.0:
            score -= 0.2
            factors.append(f"Bloated ERA ({own_starter_era:.2f})")

    # 6. Opp team OPS — small contribution (weak monotonic Q1 31% / Q4 25%)
    if opp_team_ops is not None:
        if opp_team_ops <= 0.690:
            score += 0.3
            factors.append(f"Weak opponent OPS ({opp_team_ops:.3f})")
        elif opp_team_ops <= 0.720:
            score += 0.1

    env_score = max(0.0, min(1.0, score / max_score))
    return env_score, factors


def compute_batter_env_score(
    vegas_total: float | None = None,             # V12: ignored — Q1 50%/Q4 47% HV, dead signal
    opp_pitcher_era: float | None = None,
    platoon_advantage: bool = False,
    batting_order: int | None = None,
    park_team: str | None = None,
    wind_speed_mph: float | None = None,
    wind_direction: str | None = None,
    temperature_f: int | None = None,
    team_moneyline: int | None = None,
    opp_bullpen_era: float | None = None,         # V12: ignored — non-monotonic
    series_team_wins: int | None = None,          # V12: ignored — V10.7 already neutralized
    series_opp_wins: int | None = None,           # V12: ignored
    team_l10_wins: int | None = None,             # V12: ignored — INVERTED (cold>hot for HV)
    opp_starter_whip: float | None = None,
    opp_starter_k_per_9: float | None = None,     # V12: ignored — Q1 49%/Q4 45% HV, dead
    opp_team_rest_days: int | None = None,        # V12: ignored — small N, no audit signal
) -> tuple[float, list[str], int]:
    """
    V12 batter env score (0-1.0) — calibrated against 35-slate / 994-batter
    historical audit.  Built ONLY from signals the data showed actually
    separate HV outcomes:

      1. Opp starter ERA (STRONGEST batter signal):
            Q1 (≤3.1) HV=34%, Q4 (≥5.8) HV=57% — +23pp swing, monotonic
      2. Opp starter WHIP (independent confirm):
            Q1 (≤1.11) HV=39%, Q4 (≥1.56) HV=55% — +16pp, monotonic
      3. Wind speed (REAL — survives park control):
            ≥10 mph + OUT direction: HV=66% on windy days at any park
            ≥10 mph + IN direction: HV=53%
      4. Park HR factor (modest for batters; strong interaction with wind):
            Q1 (≤0.95) HV=48%, Q4 (≥1.05) HV=51%
      5. Underdog premium (INVERTED from V11.0 expectation):
            Q1 (heavy fav -310 to -158) HV=36%
            Q4 (underdog +104 to +250) HV=57%
            Underdog teams produce MORE individual HV (star-carry mechanics).
      6. Batting order (top-of-order volume premium)
      7. Temperature (mild monotonic; hot weather lifts HV ~8pp)
      8. Platoon advantage (kept; binary)

    Signals deliberately REMOVED from V12 vs V11.0:
      - Vegas O/U (Q1 50% / Q4 47%, ZERO separation — noise)
      - Opposing bullpen ERA (non-monotonic across quartiles)
      - Opposing starter K/9 (Q1 49% / Q4 45%, dead — V10.6 add was wrong)
      - Heavy-favorite ML positive bonus (it's actually inverted)
      - Own-team L10 momentum (cold teams beat hot teams for HV)
      - Series leading/trailing (V10.7 already neutralized)
      - Opp back-to-back rest-days bonus (no audit support)
      - Compound park × temp interaction (no audit support)

    Returns env_score, factor list, and unknown_count.  Unknown_count tracks
    missing critical fields (opp ERA, opp WHIP, batting order) — used downstream
    by `_compute_dnp_adjustment` to distinguish "lineup not yet published"
    from "confirmed not starting".
    """
    factors: list[str] = []
    unknown_count = 0
    score = 0.0
    max_score = 4.0  # tuned so a "perfect storm" batter saturates near 1.0

    # 1. Opp starter ERA — STRONGEST single batter signal (+23pp HV swing)
    if opp_pitcher_era is not None:
        if opp_pitcher_era >= 6.0:
            score += 1.4
            factors.append(f"Bloated opp starter (ERA={opp_pitcher_era:.2f})")
        elif opp_pitcher_era >= 5.0:
            score += 1.1
            factors.append(f"Weak opp starter (ERA={opp_pitcher_era:.2f})")
        elif opp_pitcher_era >= 4.0:
            score += 0.7
            factors.append(f"Mediocre opp starter (ERA={opp_pitcher_era:.2f})")
        elif opp_pitcher_era >= 3.0:
            score += 0.3
        # Sub-3.0 ERA contributes 0 (don't reward elite-pitcher matchups for batters)
    else:
        unknown_count += 1

    # 2. Opp starter WHIP — second strongest, independent of ERA in the corners
    if opp_starter_whip is not None:
        if opp_starter_whip >= 1.5:
            score += 0.9
            factors.append(f"Wild opp starter (WHIP={opp_starter_whip:.2f})")
        elif opp_starter_whip >= 1.3:
            score += 0.6
            factors.append(f"Below-avg command (WHIP={opp_starter_whip:.2f})")
        elif opp_starter_whip >= 1.1:
            score += 0.3
    else:
        unknown_count += 1

    # 3. Wind speed — REAL signal (survived park-control test in audit)
    if wind_speed_mph is not None and wind_direction:
        d = wind_direction.upper()
        if wind_speed_mph >= 10:
            if any(o in d for o in BATTER_ENV_WIND_OUT_DIRECTIONS):
                score += 0.6
                factors.append(f"Wind out {wind_speed_mph:.0f} mph")
            elif any(i in d for i in BATTER_ENV_WIND_IN_DIRECTIONS):
                score += 0.1
                factors.append(f"Wind in {wind_speed_mph:.0f} mph (mild)")
            else:
                score += 0.4
                factors.append(f"Wind {wind_speed_mph:.0f} mph cross")
        elif wind_speed_mph >= 6:
            score += 0.1

    # 4. Park HR factor — modest contribution for batters
    if park_team:
        pf = PARK_HR_FACTORS.get(park_team, 1.0)
        if pf >= 1.05:
            score += 0.3
            factors.append(f"Hitter park ({park_team}, factor={pf:.2f})")
        elif pf >= 1.0:
            score += 0.1

    # 5. Moneyline — INVERTED from intuition.  Underdog batters produce MORE HV.
    if team_moneyline is not None:
        if team_moneyline >= 100:
            score += 0.3
            factors.append(f"Underdog premium (ML=+{team_moneyline}, HV+21pp historical)")
        elif -180 <= team_moneyline <= -110:
            score += 0.2
            factors.append(f"Mild fav (ML={team_moneyline})")
        elif team_moneyline <= -200:
            score -= 0.2
            factors.append(f"Heavy favorite penalty (ML={team_moneyline}, HV-21pp historical)")

    # 6. Batting order — premium for top of order (PA volume)
    if batting_order is not None:
        if batting_order <= 3:
            score += 0.4
            factors.append(f"Top of order (#{batting_order})")
        elif batting_order <= 5:
            score += 0.3
            factors.append(f"Heart of order (#{batting_order})")
        elif batting_order <= 7:
            score += 0.15
        else:
            score += 0.05
    else:
        unknown_count += 1

    # 7. Temperature — mild lift in hot weather
    if temperature_f is not None and temperature_f >= 75:
        score += 0.1

    # 8. Platoon advantage — small bonus
    if platoon_advantage:
        score += 0.3
        factors.append("Platoon advantage")

    if unknown_count > 0:
        factors.append(f"{unknown_count} unknown factor(s)")

    env_score = max(0.0, min(1.0, score / max_score))
    return env_score, factors, unknown_count



# ---------------------------------------------------------------------------
# Filter 4+5: Boost Optimization & Lineup Construction
# These are integrated into the FilterStrategyOptimizer below.
# ---------------------------------------------------------------------------

@dataclass
class FilteredCandidate:
    """A player card that has passed through the resolver into the optimizer.

    ========================================================================
    STRUCTURAL ISOLATION: card_boost, drafts, and popularity are NEVER
    present on this dataclass.  card_boost / drafts are unknowable
    pre-draft; popularity was removed in V11.0.  Removing them entirely
    (instead of marking them "display only") makes the "no ownership /
    boost / popularity signals in pre-game prediction" rule enforceable
    by static grep.  The router layer reads card_boost and drafts straight
    from the source FilterCard for display purposes.
    ========================================================================

    EV is computed from pre-game signals only:
      env_score   — Vegas O/U, opposing ERA/bullpen, park, weather, platoon, batting order
      total_score — season-level trait quality (K/9, ISO, barrel%, speed, recent form)

    See CLAUDE.md § "Signal Isolation: ABSOLUTE RULE" for full rationale.
    """
    player_name: str
    team: str
    position: str
    total_score: float    # 0-100 from scoring engine (pre-game season stats)
    env_score: float      # 0-1.0 from environmental filter (pre-game conditions)
    env_factors: list[str] = field(default_factory=list)
    env_unknown_count: int = 0  # how many env factors were missing data
    game_id: int | str | None = None  # for diversification tracking
    is_pitcher: bool = False
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
    """Compute the EV ranking signal for one candidate.

    V11.0 "Filter, Not Forecast" — EV is built exclusively from pre-game
    signals.  No RS data, no historical outcomes, no ownership counts,
    NO POPULARITY SIGNALS.  Predict high-value performers; ignore the crowd.

    Five multiplicative terms:
      1. env_factor          — PRIMARY: game conditions.  Pitchers cap at
                               PITCHER_ENV_MODIFIER_CEILING (1.20), batters
                               at ENV_MODIFIER_CEILING (1.30) — asymmetric
                               because pitcher outcomes are 1-player-dependent
                               and saturate trivially.
      2. volatility_amplifier— Boom-bust hitter amplifier.  Env-CONDITIONAL:
                               amplifies env in good matchups, penalises in
                               bad.  Pitchers always 1.0 (no recent_form_cv).
      3. trait_factor        — SECONDARY: intrinsic player quality (Statcast
                               kinematics + ERA/K9/WHIP for SP; exit-velo +
                               hard-hit% + barrel% for batters).  0.85–1.15.
      4. stack_bonus         — 1.20 if PATH 1 blowout-favorite team, else 1.0.
      5. dnp_adj             — Confirmed-bad 0.70 / unknown 0.93 / known 1.0.

    Formula:
        base_ev = env_factor × volatility_amplifier × trait_factor
                  × stack_bonus × dnp_adj × 100
    """
    raw_env = max(candidate.env_score, 0.0)
    # V10.6 (April 28-29 evaluation): asymmetric env ceiling — pitchers cap at
    # PITCHER_ENV_MODIFIER_CEILING (1.20) vs batters at ENV_MODIFIER_CEILING
    # (1.30).  Pitcher outcomes are 1-player-dependent, so over-saturating
    # pitcher EV (5 env factors trivially → 1.0 saturation → 1.30 multiplier)
    # priced batters out of the top-10 even on shootout slates.  The harness
    # showed 54% of model top-10 were pitchers (target ~40%); tightening the
    # pitcher band lets exceptional batter env situations compete.
    env_ceiling = PITCHER_ENV_MODIFIER_CEILING if candidate.is_pitcher else ENV_MODIFIER_CEILING
    env_factor = ENV_MODIFIER_FLOOR + raw_env * (env_ceiling - ENV_MODIFIER_FLOOR)
    env_factor = max(ENV_MODIFIER_FLOOR, min(env_ceiling, env_factor))

    # Volatility amplifier: high-variance batters amplify env signal both ways.
    # V10.6 (April 28-29 evaluation): made the amplifier env-CONDITIONAL.  Pre-
    # V10.6 it was unconditional `1.0 + cv × 0.20`, which always boosted volatile
    # boom-bust hitters regardless of context — that's why the live pipeline
    # systematically over-loved Max Muncy / Aaron Judge / Yordan-class profiles
    # even on slates where their actual env (cold weather, ace pitcher matchup)
    # was poor.  New formula scales the boost by env deviation from neutral:
    #
    #     amp = 1 + cv × MAX × (env_score − 0.5) × 2
    #
    # Volatile batter in great env (env=1.0) gets +20% amplification; in
    # neutral env (env=0.5) gets 1.0× (no amplification, no penalty); in
    # bad env (env=0.0) gets −20% (penalty for boom-bust profile in a matchup
    # they're likely to bust in).  Steady batters (cv ≈ 0) are unaffected
    # by this term, leaving the env signal alone to rank them.  Pitchers
    # never carry recent_form_cv, so they default to 1.0 (no change).
    volatility_amplifier = 1.0
    if candidate.traits:
        for trait in candidate.traits:
            if trait.name == "recent_form" and "recent_form_cv" in trait.metadata:
                cv = trait.metadata["recent_form_cv"]
                # Env deviation from neutral: (raw_env - 0.5) × 2 maps env [0,1]
                # to [-1, +1].  Clamp to keep extreme rookies/ghosts within band.
                env_deviation = max(-1.0, min(1.0, (raw_env - 0.5) * 2.0))
                volatility_amplifier = 1.0 + (cv * BATTER_FORM_VOLATILITY_MAX * env_deviation)
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


def _lineup_total_ev(lineup: list[FilteredCandidate]) -> float:
    """Compute the slot-weighted total EV for a lineup.

    V12: pitcher-count agnostic.  Sort all 5 candidates by filter_ev
    descending and assign them to slot multipliers 2.0, 1.8, 1.6, 1.4, 1.2.
    Highest-EV player (whether pitcher or batter) takes Slot 1.
    """
    if not lineup:
        return 0.0
    sorted_lineup = sorted(lineup, key=lambda c: c.filter_ev, reverse=True)
    slot_mults_desc = sorted(SLOT_MULTIPLIERS.values(), reverse=True)
    total = 0.0
    for player, mult in zip(sorted_lineup, slot_mults_desc):
        total += player.filter_ev * (mult / BASE_MULTIPLIER)
    return total


def _build_variant(
    n_pitchers: int,
    sorted_pitchers: list[FilteredCandidate],
    sorted_batters: list[FilteredCandidate],
    stack_eligible_teams: set[str],
) -> list[FilteredCandidate]:
    """Build a single n_pitchers + (5 - n_pitchers) batters lineup variant.

    Pitchers are picked top-EV first.  Batters are picked top-EV under per-team
    cap (1 default, 2 stack-eligible) AND the anti-correlation guard — no batter
    from a game where we already have a pitcher unless they're that pitcher's
    teammate (pitcher and his own offense are positively correlated; pitcher
    and opposing offense are negatively correlated).

    Returns [] if the pool can't fill the variant under caps.
    """
    n_batters = 5 - n_pitchers
    if len(sorted_pitchers) < n_pitchers:
        return []

    chosen_pitchers = sorted_pitchers[:n_pitchers]
    # Map of (game_id -> team_we_pitched_for) — opposing batters in those games are blocked.
    pitcher_games_to_protect = {
        p.game_id: p.team.upper()
        for p in chosen_pitchers
        if p.game_id is not None
    }

    chosen_batters: list[FilteredCandidate] = []
    team_count: dict[str, int] = {}
    game_count: dict[int | str, int] = {}
    for b in sorted_batters:
        if len(chosen_batters) == n_batters:
            break
        team_key = b.team.upper()
        # Anti-correlation: skip batters who oppose any of our drafted pitchers
        if b.game_id in pitcher_games_to_protect:
            anchor_team = pitcher_games_to_protect[b.game_id]
            if team_key != anchor_team:
                continue
        cap = _team_batter_cap(team_key, stack_eligible_teams)
        if team_count.get(team_key, 0) >= cap:
            continue
        if b.game_id is not None and game_count.get(b.game_id, 0) >= MAX_PLAYERS_PER_GAME_BATTERS:
            continue
        team_count[team_key] = team_count.get(team_key, 0) + 1
        if b.game_id is not None:
            game_count[b.game_id] = game_count.get(b.game_id, 0) + 1
        chosen_batters.append(b)

    if len(chosen_batters) < n_batters:
        return []
    return chosen_pitchers + chosen_batters


def _enforce_composition(
    candidates: list[FilteredCandidate],
    slate_class: SlateClassification,
) -> list[FilteredCandidate]:
    """
    V12 EV-driven composition: try EVERY pitcher count from 0 to 5, return
    the variant with the highest slot-weighted total EV.

    Audit of 35 slates of actual #1 winning lineups (2026-03-25 → 2026-04-28):
        2P+3B: 28.6%   ← most common winning shape
        0P+5B: 25.7%
        1P+4B: 17.1%
        3P+2B: 14.3%
        4P+1B: 11.4%
        5P+0B:  2.9%

    Pre-V12 we only built {0P+5B, 1P+4B} — structurally incapable of
    producing 57% of winning shapes.  Mean total RS by shape (winning
    lineups only): 0P=17.5, 1P=18.5, 2P=20.3, 4P=22.9, 5P=26.6.
    Pitchers score more per RS-event because individual K/win-bonus
    games stack.  The EV-driven chooser was correct in spirit; the
    structural cap of 1 pitcher was the limiter.

    Backtest of V12 with multi-pitcher variants on the same 35 slates:
    beat the actual #1 winning lineup on 57.1% of slates (vs 22.9% with
    only 0P/1P).  Mean slot-weighted RS rose from 28.65 → 35.43.

    Anti-correlation guard: a batter is blocked from any game where one
    of our drafted pitchers plays UNLESS the batter is that pitcher's
    teammate.  Stack and per-game caps still apply.

    Tiebreak: higher pitcher count wins exact ties — empirically that's
    the slate where pitcher upside is real.
    """
    sorted_all = sorted(candidates, key=lambda c: c.filter_ev, reverse=True)
    sorted_pitchers = [c for c in sorted_all if c.is_pitcher]
    sorted_batters = [c for c in sorted_all if not c.is_pitcher]
    stack_eligible_teams = _compute_stack_eligible_teams(slate_class)

    best_lineup: list[FilteredCandidate] = []
    best_ev: float = -1.0
    best_n_p: int = -1
    variant_evs: dict[int, float] = {}
    for n_p in range(0, 6):  # 0P..5P
        lineup = _build_variant(n_p, sorted_pitchers, sorted_batters, stack_eligible_teams)
        if len(lineup) != 5:
            continue
        ev = _lineup_total_ev(lineup)
        variant_evs[n_p] = ev
        # ">" not ">=" so when EVs tie the higher-pitcher variant wins
        if ev > best_ev:
            best_ev = ev
            best_lineup = lineup
            best_n_p = n_p

    if not best_lineup:
        raise ValueError(
            "Candidate pool could not produce any 0P-5P lineup variant. "
            "Cannot build a lineup."
        )

    from collections import Counter
    stack_teams = [
        t for t, n in Counter(c.team for c in best_lineup if not c.is_pitcher).items() if n >= 2
    ]
    logger.info(
        "V12 composition: chose %dP+%dB (ev=%.2f) — variant_evs=%s — "
        "stack_eligible=%s mini_stacks_used=%s (candidates: %d)",
        best_n_p, 5 - best_n_p, best_ev,
        {k: round(v, 2) for k, v in variant_evs.items()},
        sorted(stack_eligible_teams) or "none",
        stack_teams or "none", len(candidates),
    )
    return best_lineup


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

    # V12: pitcher count is unconstrained (0-5 all legal — chooser in
    # _enforce_composition picks the best variant by slot-weighted EV).

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
    """V12 slot assignment: assign all 5 candidates to slots by filter_ev
    descending, regardless of position.  Highest-EV player → Slot 1 (2.0×),
    next → Slot 2 (1.8×), etc.

    Pitcher-count agnostic: a pitcher gets Slot 1 only if his EV is highest;
    otherwise the top batter takes it.  This is the right behaviour because
    the slot multiplier compounds linearly with player EV — putting your
    highest-EV asset in the highest-multiplier slot is always optimal
    (rearrangement inequality).
    """
    if not candidates:
        return []

    slot_mults = dict(SLOT_MULTIPLIERS)  # {1: 2.0, 2: 1.8, ...}
    sorted_candidates = sorted(candidates, key=lambda c: c.filter_ev, reverse=True)
    slots_desc = sorted(slot_mults.items(), key=lambda x: x[1], reverse=True)

    assignments: list[FilterSlotAssignment] = []
    for player, (slot_idx, slot_mult) in zip(sorted_candidates, slots_desc):
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

def run_filter_strategy(
    candidates: list[FilteredCandidate],
    slate_classification: SlateClassification,
) -> FilterOptimizedLineup:
    """
    Run the full "Filter, Not Forecast" pipeline.

    This is the main entry point. Takes pre-scored candidates and
    produces an optimized lineup using only env + trait signals.

    Steps:
    1. Compute filter-adjusted EV for each candidate (env + trait + context)
    2. Enforce composition (1 pitcher + 4 batters, OR 0 pitcher + 5 batters,
       chosen by slot-weighted total EV)
    3. Check game diversification
    4. Smart slot assignment

    V11.0: no popularity gate.  Every candidate that came through the
    resolver is in the pool; ranking is pure env × trait × context.
    """
    if not candidates:
        return FilterOptimizedLineup(
            slots=[],
            total_expected_value=0.0,
            strategy="filter_not_forecast",
            slate_classification=slate_classification,
        )

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

    # Step 1: Compute filter-adjusted EV (env + trait + context)
    for c in candidates:
        c.filter_ev = _compute_filter_ev(c)

    # Step 2: Enforce composition — 1P+4B (anchor) or 0P+5B (pure-batter),
    # chosen by slot-weighted total EV.
    lineup = _enforce_composition(candidates, slate_classification)

    # Step 3: Game diversification (safety-net; team/game caps enforced upstream)
    warnings = _apply_game_diversification(lineup)

    # Step 4: Smart slot assignment
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
