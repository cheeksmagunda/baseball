"""
Rule-based scoring engine for DFS player evaluation.

Scores players 0-100 based on trait profiles derived from
Highest Value player analysis across March 25-31, 2026 data.
"""

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from app.core.constants import (
    DEFAULT_BATTER_OPS_VS_LHP,
    DEFAULT_BATTER_OPS_VS_RHP,
    DEFAULT_OPP_K_PCT,
    DEFAULT_OPP_OPS,
    DEFAULT_PITCHER_ERA,
    DEFAULT_PITCHER_WHIP,
    PARK_HR_FACTOR_MAX,
    PARK_HR_FACTOR_MIN,
    PARK_HR_FACTORS,
    PITCHER_POSITIONS,
    POWER_PROFILE_AVG_EV_MAX,
    POWER_PROFILE_BARREL_PCT_MAX,
    POWER_PROFILE_HARD_HIT_MAX,
    POWER_PROFILE_HR_PA_MAX,
    POWER_PROFILE_MAX_EV_CEILING,
    SCORING_BATTER_ERA_FLOOR,
    SCORING_BATTER_ERA_RANGE,
    SCORING_BATTER_OPS_SPLIT_FLOOR,
    SCORING_BATTER_OPS_SPLIT_RANGE,
    SCORING_BATTER_WHIP_FLOOR,
    SCORING_BATTER_WHIP_RANGE,
    SCORING_CHASE_PCT_CEILING,
    SCORING_CHASE_PCT_FLOOR,
    SCORING_ERA_CEILING,
    SCORING_ERA_RANGE,
    SCORING_FB_EXTENSION_CEILING,
    SCORING_FB_EXTENSION_FLOOR,
    SCORING_FB_IVB_CEILING,
    SCORING_FB_IVB_FLOOR,
    SCORING_FB_VELOCITY_CEILING,
    SCORING_FB_VELOCITY_FLOOR,
    SCORING_K9_CEILING,
    SCORING_K9_FLOOR,
    SCORING_PITCHER_K_PCT_FLOOR,
    SCORING_PITCHER_K_PCT_RANGE,
    SCORING_PITCHER_OPS_CEILING,
    SCORING_PITCHER_OPS_RANGE,
    SCORING_WHIFF_PCT_CEILING,
    SCORING_WHIFF_PCT_FLOOR,
    SCORING_WHIP_CEILING,
    SCORING_WHIP_RANGE,
    UNKNOWN_SCORE_RATIO,
)
from app.core.utils import get_recent_games, scale_score
from app.core.weights import ScoringWeights, get_current_weights
from app.models.player import Player, PlayerGameLog, PlayerStats


@dataclass
class TraitResult:
    name: str
    score: float
    max_score: float
    raw_value: str = ""
    metadata: dict = field(default_factory=dict)  # for trait-specific data like recent_form_cv


@dataclass
class PlayerScoreResult:
    player_name: str
    team: str
    position: str
    total_score: float
    traits: list[TraitResult]


# ---------------------------------------------------------------------------
# Pitcher trait scorers
# ---------------------------------------------------------------------------

def score_ace_status(stats: PlayerStats | None, max_pts: float) -> TraitResult:
    """Score based on pitcher quality indicators (IP, ERA as proxy for rotation rank)."""
    if not stats or stats.ip == 0:
        return TraitResult("ace_status", 0, max_pts, "no stats")

    # Use ERA as proxy: <2.5 = ace, <3.5 = solid, <4.5 = average, >4.5 = back-end
    era = stats.era or DEFAULT_PITCHER_ERA
    if era < 2.5:
        score = max_pts
    elif era < 3.0:
        score = max_pts * 0.85
    elif era < 3.5:
        score = max_pts * 0.7
    elif era < 4.0:
        score = max_pts * 0.5
    elif era < 4.5:
        score = max_pts * 0.3
    else:
        score = max_pts * 0.1

    return TraitResult("ace_status", round(score, 1), max_pts, f"ERA={era:.2f}")


def score_pitcher_k_rate(stats: PlayerStats | None, max_pts: float) -> TraitResult:
    """Score strikeout upside.

    V10.0: Statcast kinematics replace raw K/9 as the dominant signal when
    available.  Strategy doc §"Induced Vertical Break": FB velocity, IVB,
    extension, whiff %, and chase % are leading indicators; K/9 is the lagging
    outcome.  Schlittler/Abel-type rookie profiles (elite physics, shallow MLB
    sample) score high immediately instead of waiting for K/9 to stabilize.

    Blending rule:
      * 5 kinematic sub-signals each contribute 1 point (0.0–1.0 scaled).
      * If ≥3 are present, kinematic score is 70% of trait (dominant).
      * If 0–2 are present, fall back to K/9 scaling at full weight.
      * If NONE of the signals are present (true MLB-debut rookie), return
        UNKNOWN_SCORE_RATIO × max_pts so the player doesn't get mathematically
        benched — the env/popularity filters decide their fate from there.
        Rookie Arbitrage per strategy doc §"Rookie Variance Void": the crowd
        ignores MLB-debut arms; our job is to not ignore them ourselves.
    """
    if not stats:
        return TraitResult("k_rate", max_pts * UNKNOWN_SCORE_RATIO, max_pts, "no stats (rookie baseline)")

    subs: list[tuple[str, float]] = []
    if stats.fb_velocity is not None:
        subs.append(("FB_velo", scale_score(stats.fb_velocity, SCORING_FB_VELOCITY_FLOOR, SCORING_FB_VELOCITY_CEILING, 1.0)))
    if stats.fb_ivb is not None:
        subs.append(("IVB", scale_score(stats.fb_ivb, SCORING_FB_IVB_FLOOR, SCORING_FB_IVB_CEILING, 1.0)))
    if stats.fb_extension is not None:
        subs.append(("ext", scale_score(stats.fb_extension, SCORING_FB_EXTENSION_FLOOR, SCORING_FB_EXTENSION_CEILING, 1.0)))
    if stats.whiff_pct is not None:
        subs.append(("whiff%", scale_score(stats.whiff_pct, SCORING_WHIFF_PCT_FLOOR, SCORING_WHIFF_PCT_CEILING, 1.0)))
    if stats.chase_pct is not None:
        subs.append(("chase%", scale_score(stats.chase_pct, SCORING_CHASE_PCT_FLOOR, SCORING_CHASE_PCT_CEILING, 1.0)))

    if len(subs) >= 3:
        kinematic = sum(v for _, v in subs) / len(subs)  # 0.0-1.0
        if stats.k_per_9 is not None:
            k9_norm = scale_score(stats.k_per_9, SCORING_K9_FLOOR, SCORING_K9_CEILING, 1.0)
            combined = 0.70 * kinematic + 0.30 * k9_norm
        else:
            combined = kinematic
        score = combined * max_pts
        detail = " ".join(f"{n}={v:.2f}" for n, v in subs)
        return TraitResult("k_rate", round(score, 1), max_pts, detail)

    # No Statcast — fall back to K/9 scaling (covers new call-ups without Savant rows).
    if stats.k_per_9 is None:
        # True zero-data rookie (MLB debut).  Return the neutral baseline so
        # env + popularity + park can still lift them into the pool.
        return TraitResult(
            "k_rate",
            round(max_pts * UNKNOWN_SCORE_RATIO, 1),
            max_pts,
            "no K/9 data, no statcast (rookie baseline)",
        )

    score = scale_score(stats.k_per_9, SCORING_K9_FLOOR, SCORING_K9_CEILING, max_pts)
    return TraitResult("k_rate", round(score, 1), max_pts, f"K/9={stats.k_per_9:.1f} (no statcast)")


def score_pitcher_matchup(
    opp_team: str | None, opp_stats: dict | None, max_pts: float
) -> TraitResult:
    """Score based on opponent offensive quality. Weaker opponent = higher score."""
    if not opp_team or not opp_stats:
        return TraitResult("matchup_quality", max_pts * UNKNOWN_SCORE_RATIO, max_pts, "matchup unknown")

    opp_ops = opp_stats.get("ops", DEFAULT_OPP_OPS)
    opp_k_pct = opp_stats.get("k_pct", DEFAULT_OPP_K_PCT)

    # OPS component: lower is better for pitcher (inverted scale)
    ops_score = scale_score(SCORING_PITCHER_OPS_CEILING - opp_ops, 0, SCORING_PITCHER_OPS_RANGE, 1.0)
    # K% component: higher K% is better for pitcher
    k_score = scale_score(opp_k_pct - SCORING_PITCHER_K_PCT_FLOOR, 0, SCORING_PITCHER_K_PCT_RANGE, 1.0)

    combined = (ops_score * 0.6 + k_score * 0.4) * max_pts
    return TraitResult(
        "matchup_quality",
        round(combined, 1),
        max_pts,
        f"opp_OPS={opp_ops:.3f} opp_K%={opp_k_pct:.3f}",
    )


def score_pitcher_recent_form(
    game_logs: list[PlayerGameLog], max_pts: float
) -> TraitResult:
    """Score based on last 3 starts with trajectory signal. Rewards pitchers trending up."""
    if not game_logs:
        return TraitResult("recent_form", max_pts * 0.4, max_pts, "no recent games")

    recent = get_recent_games(game_logs, 3)

    def _start_quality(g) -> float:
        if g.ip >= 5.0 and g.er <= 3:
            if g.er == 0:
                return 1.0
            elif g.er <= 1:
                return 0.85
            else:
                return 0.6
        elif g.ip >= 4.0 and g.er <= 2:
            return 0.5
        else:
            return 0.15

    start_scores = [_start_quality(g) for g in recent]
    avg_score = sum(start_scores) / len(start_scores)

    # Trajectory: compare most recent start against the pitcher's own prior-starts average.
    # No static historical anchor — direction is relative to the player's own recent baseline.
    if len(start_scores) > 1:
        most_recent = start_scores[0]
        prior_avg = sum(start_scores[1:]) / len(start_scores[1:])
        if prior_avg > 0:
            if most_recent >= prior_avg * 1.15:
                traj_mult = 1.10   # trending up: +15% vs own recent baseline
            elif most_recent <= prior_avg * 0.85:
                traj_mult = 0.90   # trending down: -15% vs own recent baseline
            else:
                traj_mult = 1.0
        else:
            traj_mult = 1.0
    else:
        traj_mult = 1.0

    result = min(max_pts, avg_score * max_pts * traj_mult)
    return TraitResult(
        "recent_form",
        round(result, 1),
        max_pts,
        f"{len(recent)} starts, avg_quality={avg_score:.2f} traj={traj_mult:.2f}x",
    )


def score_pitcher_era_whip(stats: PlayerStats | None, max_pts: float) -> TraitResult:
    """Combined ERA + WHIP score."""
    if not stats:
        return TraitResult("era_whip", 0, max_pts, "no stats")

    era = stats.era or DEFAULT_PITCHER_ERA
    whip = stats.whip or DEFAULT_PITCHER_WHIP

    # ERA component: lower is better (inverted scale)
    era_score = scale_score(SCORING_ERA_CEILING - era, 0, SCORING_ERA_RANGE, 1.0)
    # WHIP component: lower is better (inverted scale)
    whip_score = scale_score(SCORING_WHIP_CEILING - whip, 0, SCORING_WHIP_RANGE, 1.0)

    combined = (era_score * 0.6 + whip_score * 0.4) * max_pts
    return TraitResult(
        "era_whip",
        round(combined, 1),
        max_pts,
        f"ERA={era:.2f} WHIP={whip:.2f}",
    )


# ---------------------------------------------------------------------------
# Batter trait scorers
# ---------------------------------------------------------------------------

def score_power_profile(stats: PlayerStats | None, max_pts: float) -> TraitResult:
    """Score power based on Statcast kinematics + HR rate.

    V10.0: rebuilt around what the target app actually rewards — distance of
    the play.  Strategy doc §"Offensive Engine": avg exit velocity, hard-hit
    %, and barrel % are the upstream signals that produce 400+ ft home runs
    and 105 mph hits.  HR/PA and max EV are kept as thin confirmation
    signals (2 pts each) — they lag the physical profile.

    Components (25-pt denominator):
      avg_exit_velocity  → 8 pts  (92 mph floor, league-avg power)
      hard_hit_pct       → 7 pts  (50% → elite sluggers)
      barrel_pct         → 6 pts  (15% → Stewart/DeLauter tier)
      max_exit_velocity  → 2 pts  (112 mph peak)
      HR/PA              → 2 pts  (retrospective confirmation)

    Rookie Arbitrage (strategy doc §"Rookie Variance Void"): a true MLB debut
    with zero plate appearances AND no Statcast row returns the neutral
    baseline (UNKNOWN_SCORE_RATIO × max_pts) rather than zero, so the env /
    popularity / park filters can still promote them.  The crowd fades
    rookies; our engine must not also fade them by default.
    """
    if not stats or stats.pa == 0:
        return TraitResult(
            "power_profile",
            round(max_pts * UNKNOWN_SCORE_RATIO, 1),
            max_pts,
            "no stats (rookie baseline)",
        )

    hr_per_pa = stats.hr / max(stats.pa, 1)
    avg_ev = stats.avg_exit_velocity
    hard_hit = stats.hard_hit_pct
    barrel_pct = stats.barrel_pct
    max_ev = stats.max_exit_velocity

    # Each sub-score is 0.0–1.0, then weighted by its point allotment.
    avg_ev_score = scale_score(avg_ev, 85.0, POWER_PROFILE_AVG_EV_MAX, 1.0) if avg_ev is not None else None
    hard_hit_score = scale_score(hard_hit, 30.0, POWER_PROFILE_HARD_HIT_MAX, 1.0) if hard_hit is not None else None
    barrel_score = scale_score(barrel_pct, 4.0, POWER_PROFILE_BARREL_PCT_MAX, 1.0) if barrel_pct is not None else None
    max_ev_score = scale_score(max_ev, 105.0, POWER_PROFILE_MAX_EV_CEILING, 1.0) if max_ev is not None else None
    hr_score = min(1.0, hr_per_pa / POWER_PROFILE_HR_PA_MAX)

    # Weighted sum. Missing sub-scores drop out of the numerator AND denominator
    # so rookies with partial Statcast coverage aren't penalised for sparse data.
    components = [
        (avg_ev_score, 8.0, "EV"),
        (hard_hit_score, 7.0, "HH%"),
        (barrel_score, 6.0, "brl%"),
        (max_ev_score, 2.0, "maxEV"),
        (hr_score, 2.0, "HR/PA"),
    ]
    weighted_sum = sum(s * w for s, w, _ in components if s is not None)
    denom = sum(w for s, w, _ in components if s is not None)
    if denom == 0:
        return TraitResult("power_profile", 0, max_pts, "no power signals")

    # Scale the realised fraction up to the full POWER_PROFILE_DENOM (25) so a
    # partial-data batter can still saturate max_pts if their present signals
    # all max out — but not arbitrarily: the denominator reflects evidence.
    total = (weighted_sum / denom) * max_pts

    detail_parts = [f"HR/PA={hr_per_pa:.3f}"]
    if avg_ev is not None:
        detail_parts.append(f"EV={avg_ev:.1f}mph")
    if hard_hit is not None:
        detail_parts.append(f"HH={hard_hit:.1f}%")
    if barrel_pct is not None:
        detail_parts.append(f"brl={barrel_pct:.1f}%")
    if max_ev is not None:
        detail_parts.append(f"maxEV={max_ev:.1f}mph")

    return TraitResult(
        "power_profile",
        round(total, 1),
        max_pts,
        " ".join(detail_parts),
    )


def score_lineup_position(batting_order: int | None, max_pts: float) -> TraitResult:
    """Score based on where they bat.

    V10.0: slots 1-4 are all maximum-volume spots (strategy doc §"Predicting
    the Unpredictable": top-half batters get the most PAs and the highest
    probability of stepping to the plate in late-inning lead-change leverage).
    Slot 1 is NOT penalised — leadoff volume is the equal of the 2-4 RBI spots.
    """
    if batting_order is None:
        return TraitResult("lineup_position", max_pts * UNKNOWN_SCORE_RATIO, max_pts, "lineup unknown")

    if batting_order in (1, 2, 3, 4):
        score = max_pts
    elif batting_order == 5:
        score = max_pts * 0.8
    elif batting_order in (6, 7):
        score = max_pts * 0.5
    else:
        score = max_pts * 0.25

    return TraitResult("lineup_position", round(score, 1), max_pts, f"bats #{batting_order}")


def score_batter_matchup(
    opp_pitcher_stats: dict | None,
    batter_hand: str | None,
    max_pts: float,
    starter_hand: str | None = None,
    batter_stats: PlayerStats | None = None,
) -> TraitResult:
    """Score matchup vs opposing starter. Higher opponent ERA = better for batter.

    When starter_hand and batter splits (ops_vs_lhp / ops_vs_rhp) are available,
    blends the batter's handedness-specific OPS into the score for a more direct
    conditional sensitivity signal.  Falls back to ERA/WHIP-only when splits are
    unavailable or the batter is ambidextrous (S).
    """
    if not opp_pitcher_stats:
        return TraitResult("matchup_quality", max_pts * UNKNOWN_SCORE_RATIO, max_pts, "matchup unknown")

    opp_era = opp_pitcher_stats.get("era")
    opp_whip = opp_pitcher_stats.get("whip")
    if opp_era is None or opp_whip is None:
        return TraitResult("matchup_quality", max_pts * UNKNOWN_SCORE_RATIO, max_pts, "matchup unknown")

    # Opponent ERA: higher is better for batter
    era_score = scale_score(opp_era - SCORING_BATTER_ERA_FLOOR, 0, SCORING_BATTER_ERA_RANGE, 1.0)
    # Opponent WHIP: higher is better for batter
    whip_score = scale_score(opp_whip - SCORING_BATTER_WHIP_FLOOR, 0, SCORING_BATTER_WHIP_RANGE, 1.0)

    detail = f"vs_ERA={opp_era:.2f} vs_WHIP={opp_whip:.2f}"

    # Handedness-specific OPS split: direct conditional sensitivity signal.
    # Uses the batter's actual season OPS vs this pitcher handedness; falls back
    # to league-average default when splits are absent.  Switch hitters (S) are
    # skipped — they optimally face both hands and don't carry a single split.
    ops_split_score = None
    if starter_hand and batter_hand and batter_hand != "S":
        if starter_hand == "L":
            batter_ops = (
                batter_stats.ops_vs_lhp
                if batter_stats and batter_stats.ops_vs_lhp is not None
                else DEFAULT_BATTER_OPS_VS_LHP
            )
            ops_split_score = scale_score(batter_ops - SCORING_BATTER_OPS_SPLIT_FLOOR, 0, SCORING_BATTER_OPS_SPLIT_RANGE, 1.0)
            detail += f" [vs-LHP ops={batter_ops:.3f}]"
        elif starter_hand == "R":
            batter_ops = (
                batter_stats.ops_vs_rhp
                if batter_stats and batter_stats.ops_vs_rhp is not None
                else DEFAULT_BATTER_OPS_VS_RHP
            )
            ops_split_score = scale_score(batter_ops - SCORING_BATTER_OPS_SPLIT_FLOOR, 0, SCORING_BATTER_OPS_SPLIT_RANGE, 1.0)
            detail += f" [vs-RHP ops={batter_ops:.3f}]"

    if ops_split_score is not None:
        # Three-signal blend: pitcher ERA (40%), pitcher WHIP (25%), batter split (35%)
        combined = (era_score * 0.40 + whip_score * 0.25 + ops_split_score * 0.35) * max_pts
    else:
        combined = (era_score * 0.6 + whip_score * 0.4) * max_pts

    return TraitResult("matchup_quality", round(combined, 1), max_pts, detail)


def score_batter_recent_form(
    game_logs: list[PlayerGameLog], max_pts: float
) -> TraitResult:
    """Score last 7 games with trajectory weighting.

    Primary signal is last 2 games (who they are right now). A trajectory
    multiplier rewards players climbing toward their peak vs those already on
    the way down. Ceiling at 0.65 so only genuinely hot stretches hit max.

    Also computes coefficient of variation (CV) of per-game production as a
    volatility signal for env amplification. High CV = sensitive to conditions.
    """
    if not game_logs:
        return TraitResult("recent_form", max_pts * 0.4, max_pts, "no recent games", {})

    recent7 = get_recent_games(game_logs, 7)
    window_new = recent7[:2]   # most recent 2 — primary signal
    window_old = recent7[2:]   # prior 5 — trend baseline

    def _production(games: list) -> float:
        if not games:
            return 0.0
        h = sum(g.hits for g in games)
        ab = sum(g.ab for g in games) or 1
        hr = sum(g.hr for g in games)
        rbi = sum(g.rbi for g in games)
        return (h / ab) + (hr * 0.05) + (rbi * 0.02)

    prod_new = _production(window_new)
    prod_old = _production(window_old)

    # Compute per-game production for volatility analysis
    per_game_prod = []
    for g in recent7:
        ab = g.ab or 1
        prod = (g.hits / ab) + (g.hr * 0.05) + (g.rbi * 0.02)
        per_game_prod.append(prod)

    # Coefficient of variation (volatility) — player's own recent window, no historical anchor.
    # CV = std / mean of the same 7-game sample: pure within-window variance measure.
    if per_game_prod:
        mean_prod = sum(per_game_prod) / len(per_game_prod)
        variance = sum((p - mean_prod) ** 2 for p in per_game_prod) / len(per_game_prod)
        std_prod = variance ** 0.5
        cv = std_prod / mean_prod if mean_prod > 0 else 0.0
    else:
        cv = 0.0

    # Base score off last 3 games; harder ceiling (0.65) filters out average hot streaks
    base_score = min(max_pts, prod_new / 0.65 * max_pts)

    # Trajectory: compare recent 2-game production against the player's own prior 5-game window.
    # No historical constant — ratio is relative to the player's own recent baseline.
    # Falls back to neutral (1.0) when no prior window is available.
    if prod_old > 0:
        ratio = prod_new / prod_old
    else:
        ratio = 1.0
    if ratio >= 1.30:
        traj_mult = 1.15   # clearly ascending
    elif ratio >= 1.10:
        traj_mult = 1.08   # trending up
    elif ratio <= 0.70:
        traj_mult = 0.85   # clearly declining
    elif ratio <= 0.90:
        traj_mult = 0.92   # slightly declining
    else:
        traj_mult = 1.0

    score = min(max_pts, round(base_score * traj_mult, 1))

    all_h = sum(g.hits for g in recent7)
    all_ab = sum(g.ab for g in recent7) or 1
    all_hr = sum(g.hr for g in recent7)
    all_rbi = sum(g.rbi for g in recent7)
    return TraitResult(
        "recent_form",
        score,
        max_pts,
        f"L3_prod={prod_new:.3f} prev4_prod={prod_old:.3f} traj={traj_mult:.2f}x"
        f" | 7G: {all_h}/{all_ab} {all_hr}HR {all_rbi}RBI",
        {"recent_form_cv": cv},
    )


def score_ballpark_factor(
    park_team: str | None,
    max_pts: float,
    wind_speed_mph: float | None = None,
    wind_direction: str | None = None,
    temperature_f: int | None = None,
) -> TraitResult:
    """Score based on home ballpark HR factor, dynamically adjusted for weather.

    Wind blowing out increases the effective park factor (balls carry further).
    Wind blowing in decreases it (suppresses fly balls).  Temperature above 80°F
    also gives a small boost (warmer air is less dense).

    This is critical for parks like Wrigley Field whose factor swings wildly
    depending on wind off Lake Michigan:
      - CHC base factor = 1.06
      - Wind blowing out 15 mph → effective ~1.16
      - Wind blowing in 15 mph → effective ~0.96 (pitcher's park)
    """
    if not park_team:
        return TraitResult("ballpark_factor", max_pts * UNKNOWN_SCORE_RATIO, max_pts, "park unknown")

    base_factor = PARK_HR_FACTORS.get(park_team, 1.0)
    adjustment = 0.0
    notes = []

    if wind_speed_mph is not None and wind_speed_mph >= 5 and wind_direction:
        direction_upper = wind_direction.upper()
        # Wind intensity: scale from 5 mph (minimal) to 20 mph (max effect)
        wind_intensity = min(1.0, (wind_speed_mph - 5.0) / 15.0)

        if direction_upper == "OUT":
            # Wind blowing out — balls carry further, raises HR factor
            adjustment += 0.10 * wind_intensity
            notes.append(f"wind out +{adjustment:.2f}")
        elif direction_upper == "IN":
            # Wind blowing in — suppresses fly balls
            adjustment -= 0.10 * wind_intensity
            notes.append(f"wind in {adjustment:.2f}")

    if temperature_f is not None and temperature_f >= 80:
        # Hot air is less dense, balls carry further
        temp_boost = min(0.04, (temperature_f - 80) / 250)
        adjustment += temp_boost
        notes.append(f"temp +{temp_boost:.2f}")

    effective_factor = base_factor + adjustment
    note_str = f"park={park_team} base={base_factor:.2f} eff={effective_factor:.2f}"
    if notes:
        note_str += f" ({', '.join(notes)})"

    score = scale_score(effective_factor, PARK_HR_FACTOR_MIN, PARK_HR_FACTOR_MAX, max_pts)
    return TraitResult("ballpark_factor", round(score, 1), max_pts, note_str)


def score_hot_streak(game_logs: list[PlayerGameLog], max_pts: float) -> TraitResult:
    """Count multi-hit games in last 3 days."""
    if not game_logs:
        return TraitResult("hot_streak", 0, max_pts, "no recent games")

    recent = get_recent_games(game_logs, 3)
    multi_hit = sum(1 for g in recent if g.hits >= 2)

    if multi_hit >= 3:
        score = max_pts
    elif multi_hit == 2:
        score = max_pts * 0.7
    elif multi_hit == 1:
        score = max_pts * 0.4
    else:
        score = 0

    return TraitResult("hot_streak", round(score, 1), max_pts, f"{multi_hit}/3 multi-hit days")


def score_speed_component(stats: PlayerStats | None, max_pts: float) -> TraitResult:
    """Score stolen base potential."""
    if not stats:
        return TraitResult("speed_component", 0, max_pts, "no stats")

    games = max(stats.games or 0, 1)
    sb_pace = stats.sb / games * 162  # Project to full season

    if sb_pace >= 30:
        score = max_pts
    elif sb_pace >= 20:
        score = max_pts * 0.8
    elif sb_pace >= 10:
        score = max_pts * 0.5
    elif sb_pace >= 5:
        score = max_pts * 0.3
    else:
        score = max_pts * 0.1

    return TraitResult("speed_component", round(score, 1), max_pts, f"SB_pace={sb_pace:.0f}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def score_pitcher(
    player: Player,
    stats: PlayerStats | None,
    game_logs: list[PlayerGameLog],
    opp_team: str | None = None,
    opp_team_stats: dict | None = None,
    weights: ScoringWeights | None = None,
) -> PlayerScoreResult:
    """Score a pitcher on all traits."""
    w = (weights or ScoringWeights()).pitcher

    traits = [
        score_ace_status(stats, w.ace_status),
        score_pitcher_k_rate(stats, w.k_rate),
        score_pitcher_matchup(opp_team, opp_team_stats, w.matchup_quality),
        score_pitcher_recent_form(game_logs, w.recent_form),
        score_pitcher_era_whip(stats, w.era_whip),
    ]

    total = sum(t.score for t in traits)

    return PlayerScoreResult(
        player_name=player.name,
        team=player.team,
        position=player.position,
        total_score=round(total, 1),
        traits=traits,
    )


def score_batter(
    player: Player,
    stats: PlayerStats | None,
    game_logs: list[PlayerGameLog],
    batting_order: int | None = None,
    opp_pitcher_stats: dict | None = None,
    park_team: str | None = None,
    weights: ScoringWeights | None = None,
    wind_speed_mph: float | None = None,
    wind_direction: str | None = None,
    temperature_f: int | None = None,
    starter_hand: str | None = None,
) -> PlayerScoreResult:
    """Score a batter on all traits."""
    w = (weights or ScoringWeights()).batter

    traits = [
        score_power_profile(stats, w.power_profile),
        score_lineup_position(batting_order, w.lineup_position),
        score_batter_matchup(opp_pitcher_stats, player.bat_side, w.matchup_quality, starter_hand=starter_hand, batter_stats=stats),
        score_batter_recent_form(game_logs, w.recent_form),
        score_ballpark_factor(park_team, w.ballpark_factor, wind_speed_mph, wind_direction, temperature_f),
        score_hot_streak(game_logs, w.hot_streak),
        score_speed_component(stats, w.speed_component),
    ]

    total = sum(t.score for t in traits)

    return PlayerScoreResult(
        player_name=player.name,
        team=player.team,
        position=player.position,
        total_score=round(total, 1),
        traits=traits,
    )



# estimate_rs_probability REMOVED — it accepted card_boost as an input,
# but card_boost is only revealed during/after the draft.  The scoring
# engine runs pre-game and must not depend on during-draft variables.
# The function was also dead code (never called anywhere in the codebase).


def score_player(
    db: Session,
    player: Player,
    game_date: date | None = None,
    opp_team: str | None = None,
    opp_team_stats: dict | None = None,
    opp_pitcher_stats: dict | None = None,
    batting_order: int | None = None,
    park_team: str | None = None,
    wind_speed_mph: float | None = None,
    wind_direction: str | None = None,
    temperature_f: int | None = None,
    is_pitcher: bool | None = None,
    starter_hand: str | None = None,
) -> PlayerScoreResult:
    """Score any player (auto-detects pitcher vs batter, override with is_pitcher).

    The is_pitcher override is required for two-way players (e.g. Ohtani) whose
    DB position is 'DH' but who are confirmed starters for today's game.
    Without the override, score_player routes them to score_batter, producing
    batter traits (power_profile, etc.) while the caller expects pitcher traits
    (k_rate, ace_status, etc.), silently corrupting their EV calculation.
    """
    from app.config import settings

    weights = get_current_weights(db)
    stats = (
        db.query(PlayerStats)
        .filter_by(player_id=player.id, season=settings.current_season)
        .first()
    )
    game_logs = (
        db.query(PlayerGameLog)
        .filter_by(player_id=player.id, source="mlb_api")
        .order_by(PlayerGameLog.game_date.desc())
        .limit(10)
        .all()
    )

    # Caller override takes precedence; fall back to DB position.
    if is_pitcher is None:
        is_pitcher = player.position in PITCHER_POSITIONS

    if is_pitcher:
        return score_pitcher(
            player, stats, game_logs,
            opp_team=opp_team,
            opp_team_stats=opp_team_stats,
            weights=weights,
        )
    else:
        return score_batter(
            player, stats, game_logs,
            batting_order=batting_order,
            opp_pitcher_stats=opp_pitcher_stats,
            park_team=park_team or opp_team,
            weights=weights,
            wind_speed_mph=wind_speed_mph,
            wind_direction=wind_direction,
            temperature_f=temperature_f,
            starter_hand=starter_hand,
        )
