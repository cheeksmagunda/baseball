"""
Rule-based scoring engine for DFS player evaluation.

Scores players 0-100 based on trait profiles derived from
Highest Value player analysis across March 25-31, 2026 data.
"""

import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from app.core.constants import (
    PITCHER_POSITIONS,
    OFFENSIVE_PROFILE_OPS_CEILING,
    OFFENSIVE_PROFILE_OPS_FLOOR,
    POWER_PROFILE_AVG_EV_MAX,
    POWER_PROFILE_BARREL_PCT_MAX,
    POWER_PROFILE_HARD_HIT_MAX,
    POWER_PROFILE_X_WOBA_CEILING,
    POWER_PROFILE_X_WOBA_FLOOR,
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
    SCORING_FRAMING_K_RATE_MAX_ADJ,
    SCORING_FRAMING_RUNS_CEILING,
    SCORING_FRAMING_RUNS_FLOOR,
    SCORING_K9_CEILING,
    SCORING_K9_FLOOR,
    SCORING_WHIFF_PCT_CEILING,
    SCORING_WHIFF_PCT_FLOOR,
    SCORING_WHIP_CEILING,
    SCORING_WHIP_RANGE,
)
from app.core.utils import get_recent_games, scale_score
from app.core.weights import ScoringWeights
from app.models.player import Player, PlayerGameLog, PlayerStats

logger = logging.getLogger(__name__)


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
        raise RuntimeError(
            "ace_status called with no stats or 0 IP — upstream DNP filter "
            "should have excluded this pitcher"
        )

    # Use ERA as proxy: <2.5 = ace, <3.5 = solid, <4.5 = average, >4.5 = back-end
    if stats.era is None:
        raise RuntimeError(
            f"ERA is None for pitcher with {stats.ip:.1f} IP — data collection failure"
        )
    era = stats.era
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

    return TraitResult("ace_status", round(score, 1), max_pts, f"ERA {era:.2f} | {stats.ip:.1f} IP")


def score_pitcher_k_rate(
    stats: PlayerStats | None,
    max_pts: float,
    team_framing_runs: float | None = None,
) -> TraitResult:
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
      * If NONE of the signals are present AND k_per_9 is None, raise.
        A "true MLB-debut rookie" with zero kinematic data and zero K/9
        is not scoreable from live data — they don't belong in the pool.
        The DNP filter excludes them upstream; reaching here is a bug.

    V10.8: catcher framing adjustment.  When `team_framing_runs` is provided
    (the pitcher's team's season framing aggregate from TeamSeasonStats),
    the final k_rate score is scaled by 1 ± up to 5%, depending on how much
    the team's catcher cohort is adding (or subtracting) called strikes.
    Reduced magnitude under the 2026 ABS Challenge System but still
    meaningful for the ~98% of unchallenged pitches.  Per the framing
    research model: each framing run/game ≈ 3.9% K-rate impact; we cap at
    ±5% conservatively because the season-aggregate runs is a coarser
    signal than per-game rates.
    """
    if not stats:
        raise RuntimeError(
            "score_pitcher_k_rate called with stats=None — "
            "upstream filter should have excluded this pitcher"
        )

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
        stat_parts = []
        if stats.fb_velocity is not None:
            stat_parts.append(f"{stats.fb_velocity:.1f} mph")
        if stats.fb_ivb is not None:
            stat_parts.append(f"{stats.fb_ivb:.1f}in IVB")
        if stats.fb_extension is not None:
            stat_parts.append(f"{stats.fb_extension:.1f}ft ext")
        if stats.whiff_pct is not None:
            stat_parts.append(f"{stats.whiff_pct:.0f}% whiff")
        if stats.chase_pct is not None:
            stat_parts.append(f"{stats.chase_pct:.0f}% chase")
        if stats.k_per_9 is not None:
            stat_parts.append(f"K/9 {stats.k_per_9:.1f}")
        return TraitResult("k_rate", round(score, 1), max_pts, " | ".join(stat_parts))

    # No Statcast — fall back to K/9 scaling.
    # k_per_9 is always set when ip > 0 (data_collection.py computes it from SO/IP).
    # Reaching here with k_per_9 = None means ip == 0, which the upstream filter
    # excludes. If we get here anyway, it is a data integrity error.
    if stats.k_per_9 is None:
        raise RuntimeError(
            f"k_per_9 is None for pitcher with ip={stats.ip} — "
            "upstream filter should have excluded this player"
        )

    score = scale_score(stats.k_per_9, SCORING_K9_FLOOR, SCORING_K9_CEILING, max_pts)
    score = _apply_framing_adjustment(score, max_pts, team_framing_runs)
    return TraitResult("k_rate", round(score, 1), max_pts, f"K/9 {stats.k_per_9:.1f} (no Statcast)")


def _apply_framing_adjustment(
    score: float, max_pts: float, team_framing_runs: float | None
) -> float:
    """V10.8 — apply ±max% scaling to a pitcher k_rate score based on team framing.

    Linear in the runs value, clamped at SCORING_FRAMING_RUNS_FLOOR/CEILING.
    Score is then clamped to [0, max_pts] so we never push a 0-score score
    negative or a near-max score above max_pts.

    `team_framing_runs` is mandatory — Baseball Savant is a hard T-65
    dependency (the Statcast refresh populates TeamSeasonStats for all 30
    teams).  A None here means the Savant scrape silently dropped a team or
    the candidate's team abbreviation didn't match any TeamSeasonStats row.
    """
    if team_framing_runs is None:
        raise RuntimeError(
            "framing adjustment: team_framing_runs is None — Savant refresh "
            "must populate TeamSeasonStats for every team in the slate"
        )
    # Map runs to a -max..+max scaling in linear space.
    if team_framing_runs >= SCORING_FRAMING_RUNS_CEILING:
        adj = SCORING_FRAMING_K_RATE_MAX_ADJ
    elif team_framing_runs <= SCORING_FRAMING_RUNS_FLOOR:
        adj = -SCORING_FRAMING_K_RATE_MAX_ADJ
    else:
        # Linear interpolation through 0 → no adjustment at 0 framing runs.
        if team_framing_runs >= 0:
            ratio = team_framing_runs / SCORING_FRAMING_RUNS_CEILING
        else:
            ratio = team_framing_runs / abs(SCORING_FRAMING_RUNS_FLOOR)
        adj = ratio * SCORING_FRAMING_K_RATE_MAX_ADJ
    adjusted = score * (1.0 + adj)
    return max(0.0, min(max_pts, adjusted))



def score_pitcher_recent_form(
    game_logs: list[PlayerGameLog], max_pts: float
) -> TraitResult:
    """Score based on last 3 starts with trajectory signal. Rewards pitchers trending up."""
    if not game_logs:
        raise RuntimeError(
            "pitcher_recent_form called with empty game_logs — upstream DNP "
            "filter should have excluded this pitcher (no MLB starts on record)"
        )

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
    traj_str = "↑" if traj_mult > 1.0 else ("↓" if traj_mult < 1.0 else "→")
    start_lines = [f"{g.ip:.1f}IP/{g.er}ER/{g.k_pitching}K" for g in recent]
    return TraitResult(
        "recent_form",
        round(result, 1),
        max_pts,
        f"L{len(recent)}: {', '.join(start_lines)} {traj_str}",
    )


def score_pitcher_era_whip(stats: PlayerStats | None, max_pts: float) -> TraitResult:
    """Combined ERA + WHIP score.

    V12.2: removed xERA blend.  V12 audit (35-slate quartile bucketing)
    showed xERA produces near-identical bucket distributions to ERA
    (Q1 25.5% HV / Q4 28.6% HV — flat, vs ERA Q1 25.5% / Q4 33.9% — small
    monotonic).  xERA is algebraically related to xwOBA-against and adds
    no independent signal beyond raw ERA.  V10.8 had it at 25% weight;
    removed.

    Blend: ERA 60% + WHIP 40% (the pre-V10.8 blend, restored).
    """
    if not stats or stats.ip == 0:
        raise RuntimeError(
            "era_whip called with no stats or 0 IP — upstream DNP filter "
            "should have excluded this pitcher"
        )

    if stats.era is None or stats.whip is None:
        raise RuntimeError(
            f"ERA or WHIP is None for pitcher with {stats.ip:.1f} IP — data collection failure"
        )
    era = stats.era
    whip = stats.whip

    # ERA + WHIP both inverted (lower is better)
    era_score = scale_score(SCORING_ERA_CEILING - era, 0, SCORING_ERA_RANGE, 1.0)
    whip_score = scale_score(SCORING_WHIP_CEILING - whip, 0, SCORING_WHIP_RANGE, 1.0)
    combined = (era_score * 0.6 + whip_score * 0.4) * max_pts

    return TraitResult(
        "era_whip",
        round(combined, 1),
        max_pts,
        f"ERA {era:.2f} | WHIP {whip:.2f}",
    )


# ---------------------------------------------------------------------------
# Batter trait scorers
# ---------------------------------------------------------------------------

def score_offensive_profile(stats: PlayerStats | None, max_pts: float) -> TraitResult:
    """Score holistic offensive output: OPS-anchored season aggregate plus
    Statcast kinematics for upside confirmation.

    V13.1 sub-signals (out of POWER_PROFILE_DENOM = 30 sub-pts, scaled to
    `max_pts`):
      OPS                 → 10 pts  (ANCHOR — outcome-side, captures both
                                     on-base and slugging in one number)
      x_woba              →  7 pts  (Statcast contact-quality leading indicator)
      hard_hit_pct        →  5 pts
      barrel_pct          →  4 pts
      avg_exit_velocity   →  4 pts
      (max_exit_velocity DROPPED — 1pt of noise, redundant with hard_hit/barrel)

    Why OPS, not ISO: OPS = OBP + SLG, which already captures slugging.
    Adding ISO (= SLG − AVG) would double-count power exactly in the
    direction we're trying to balance against.

    Strict policy: OPS is required.  The DNP filter (`is_player_scoreable`)
    excludes any batter with `ops is None` upstream, so reaching this
    function with `stats.ops is None` is a data-collection failure and
    raises.  Statcast sub-signals retain the existing re-normalisation
    pattern (they're allowed to be None for new call-ups whose Savant rows
    haven't populated yet — the DNP filter requires only at least one).
    """
    if not stats or stats.pa == 0:
        raise RuntimeError(
            "offensive_profile called with no stats or 0 PA for batter — "
            "upstream filter should have excluded this player"
        )
    if stats.ops is None:
        raise RuntimeError(
            "offensive_profile: OPS is None for batter — "
            "DNP filter should have excluded this player"
        )

    ops = stats.ops
    avg_ev = stats.avg_exit_velocity
    hard_hit = stats.hard_hit_pct
    barrel_pct = stats.barrel_pct
    x_woba = stats.x_woba
    # max_ev intentionally not read — dropped in V13.1.

    # Each sub-score is 0.0–1.0, then weighted by its point allotment.
    ops_score = scale_score(ops, OFFENSIVE_PROFILE_OPS_FLOOR, OFFENSIVE_PROFILE_OPS_CEILING, 1.0)
    avg_ev_score = scale_score(avg_ev, 85.0, POWER_PROFILE_AVG_EV_MAX, 1.0) if avg_ev is not None else None
    hard_hit_score = scale_score(hard_hit, 30.0, POWER_PROFILE_HARD_HIT_MAX, 1.0) if hard_hit is not None else None
    barrel_score = scale_score(barrel_pct, 4.0, POWER_PROFILE_BARREL_PCT_MAX, 1.0) if barrel_pct is not None else None
    x_woba_score = scale_score(x_woba, POWER_PROFILE_X_WOBA_FLOOR, POWER_PROFILE_X_WOBA_CEILING, 1.0) if x_woba is not None else None

    # Weighted sum. Missing Statcast sub-scores drop out of the numerator AND
    # denominator so call-ups with partial Savant coverage aren't penalised
    # for sparse kinematics data.  OPS is always present (strict).
    components = [
        (ops_score, 10.0, "OPS"),
        (x_woba_score, 7.0, "xwOBA"),
        (hard_hit_score, 5.0, "HH%"),
        (barrel_score, 4.0, "brl%"),
        (avg_ev_score, 4.0, "EV"),
    ]
    weighted_sum = sum(s * w for s, w, _ in components if s is not None)
    denom = sum(w for s, w, _ in components if s is not None)
    # denom >= 10 always (OPS is required).

    total = (weighted_sum / denom) * max_pts

    detail_parts = [f"{ops:.3f} OPS"]
    if x_woba is not None:
        detail_parts.append(f"{x_woba:.3f} xwOBA")
    if hard_hit is not None:
        detail_parts.append(f"{hard_hit:.0f}% hard-hit")
    if barrel_pct is not None:
        detail_parts.append(f"{barrel_pct:.0f}% barrel")
    if avg_ev is not None:
        detail_parts.append(f"{avg_ev:.1f} avg EV")

    return TraitResult(
        "offensive_profile",
        round(total, 1),
        max_pts,
        " ".join(detail_parts),
    )




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
        raise RuntimeError(
            "batter_recent_form called with empty game_logs — upstream DNP "
            "filter should have excluded this batter (no MLB games on record)"
        )

    recent7 = get_recent_games(game_logs, 7)
    window_new = recent7[:2]   # most recent 2 — primary signal
    window_old = recent7[2:]   # prior 5 — trend baseline

    def _production(games: list) -> float:
        if not games:
            return 0.0
        h = sum(g.hits for g in games)
        ab = sum(g.ab for g in games)
        if ab == 0:
            # Genuine zero-AB stretch (all-walk window or pinch-runner only).
            # h must also be 0 here — assert and return 0 contribution.
            assert h == 0, f"recent_form: hits={h} with ab=0 — log integrity error"
            return 0.0
        hr = sum(g.hr for g in games)
        rbi = sum(g.rbi for g in games)
        return (h / ab) + (hr * 0.05) + (rbi * 0.02)

    prod_new = _production(window_new)
    prod_old = _production(window_old)

    # Per-game production for volatility analysis. A 0-AB game contributes 0
    # production (definitionally — no chance to hit), not a fake "1 AB" denom.
    per_game_prod = []
    for g in recent7:
        if g.ab == 0:
            assert g.hits == 0, f"recent_form: game hits={g.hits} with ab=0"
            per_game_prod.append(0.0)
            continue
        prod = (g.hits / g.ab) + (g.hr * 0.05) + (g.rbi * 0.02)
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
    all_ab = sum(g.ab for g in recent7)
    all_hr = sum(g.hr for g in recent7)
    all_rbi = sum(g.rbi for g in recent7)
    traj_str = "↑" if traj_mult > 1.0 else ("↓" if traj_mult < 1.0 else "→")
    return TraitResult(
        "recent_form",
        score,
        max_pts,
        f"7G: {all_h}/{all_ab} {all_hr}HR {all_rbi}RBI {traj_str}",
        {"recent_form_cv": cv},
    )



def score_hot_streak(game_logs: list[PlayerGameLog], max_pts: float) -> TraitResult:
    """Count multi-hit games in last 3 days."""
    if not game_logs:
        raise RuntimeError(
            "hot_streak called with empty game_logs — upstream DNP filter "
            "should have excluded this batter"
        )

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
        raise RuntimeError(
            "speed_component called with no stats — upstream DNP filter "
            "should have excluded this batter"
        )

    if not stats.games or stats.games == 0:
        raise RuntimeError(
            "speed_component: batter has stats but games=0 — upstream DNP "
            "filter should have excluded a player with no game appearances"
        )
    sb_pace = stats.sb / stats.games * 162  # Project to full season

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

    return TraitResult("speed_component", round(score, 1), max_pts, f"{sb_pace:.0f} SB pace / 162G")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# V13.2 — Rookie scoring track.  When PlayerStats.is_rookie_track is True
# the player is a true MLB debutant (no current-season + no prior-season
# stats).  The traditional trait scorers above all raise on missing
# ERA/WHIP/K9 / OPS, which is correct for veterans (a missing stat there
# is a real data-collection bug) but wrong for debutants.  Rookies are
# scored separately below: trait_factor is forced to neutral (1.0) by
# returning total_score = ROOKIE_NEUTRAL_SCORE so their EV is decided
# entirely by env_factor + stack_bonus + dnp_adj.  No league-average
# defaults — the rookie has no traditional stats to score on, period.
#
# The neutral score is calibrated so that
#   trait_factor = TRAIT_MODIFIER_FLOOR + (raw_trait - 0.15) × 0.30 / 0.85
# returns 1.0 — i.e. raw_trait = 0.575, total_score = 57.5 / 100.
# See app/services/filter_strategy.py:865-870 for the lerp.
ROOKIE_NEUTRAL_SCORE = 57.5


def score_rookie(player: Player) -> PlayerScoreResult:
    """Score a true MLB debutant (V13.2).

    Returns the neutral score that maps to trait_factor = 1.0 in the EV
    formula, with no traits.  The optimizer's EV for this player becomes
    purely env-driven: env_factor × 1.0 × stack_bonus × dnp_adj × 100.
    Empirically the crowd fades rookies and the optimizer should treat
    them as "decided by environment" until they accumulate enough MLB
    stats to cross the rookie threshold and rejoin the traditional track.
    """
    return PlayerScoreResult(
        player_name=player.name,
        team=player.team,
        position=player.position,
        total_score=ROOKIE_NEUTRAL_SCORE,
        traits=[],
    )


def score_pitcher(
    player: Player,
    stats: PlayerStats | None,
    game_logs: list[PlayerGameLog],
    weights: ScoringWeights | None = None,
    team_framing_runs: float | None = None,
) -> PlayerScoreResult:
    """Score a pitcher on all traits.

    V10.8 — `team_framing_runs` is the pitcher's own team's catcher-framing
    aggregate (TeamSeasonStats.framing_runs) for the season.  When present,
    `score_pitcher_k_rate` applies a small ±5% scaling to the K-rate trait
    based on how much the team's catchers add (or subtract) called strikes.
    Reduced impact under 2026 ABS but still meaningful for unchallenged
    pitches.  None → no adjustment (default safe).
    """
    w = (weights or ScoringWeights()).pitcher

    traits = [
        score_ace_status(stats, w.ace_status),
        score_pitcher_k_rate(stats, w.k_rate, team_framing_runs=team_framing_runs),
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
    weights: ScoringWeights | None = None,
) -> PlayerScoreResult:
    """Score a batter on all traits."""
    w = (weights or ScoringWeights()).batter

    traits = [
        score_offensive_profile(stats, w.offensive_profile),
        score_batter_recent_form(game_logs, w.recent_form),
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
    is_pitcher: bool | None = None,
    team_framing_runs: float | None = None,
) -> PlayerScoreResult:
    """Score any player (auto-detects pitcher vs batter, override with is_pitcher).

    The is_pitcher override is required for two-way players (e.g. Ohtani) whose
    DB position is 'DH' but who are confirmed starters for today's game.
    Without the override, score_player routes them to score_batter, producing
    batter traits (offensive_profile, etc.) while the caller expects pitcher traits
    (k_rate, ace_status, etc.), silently corrupting their EV calculation.
    """
    from app.config import settings

    weights = ScoringWeights()
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

    if stats is None:
        # No defaults exist post-strict-mode (May 2026): the trait scorers
        # raise on missing inputs.  This branch is reachable only if a
        # caller bypassed `is_player_scoreable`; logged for diagnostic
        # value before the inevitable downstream raise.
        logger.debug(
            "score_player: no season-%d stats for %s (%s) — will raise downstream",
            settings.current_season, player.name, player.team,
        )

    # Caller override takes precedence; fall back to DB position.
    if is_pitcher is None:
        is_pitcher = player.position in PITCHER_POSITIONS

    # V13.2 — true MLB debutants are scored separately on a neutral track
    # so missing traditional stats don't crash the trait scorers.
    if stats is not None and stats.is_rookie_track:
        logger.info(
            "score_player: %s (%s, %s) on rookie scoring track — "
            "neutral total_score, env-driven EV.",
            player.name, player.team, player.position,
        )
        return score_rookie(player)

    logger.debug(
        "scoring %s (%s, %s) as_pitcher=%s stats=%s game_logs=%d",
        player.name, player.team, player.position, is_pitcher,
        "yes" if stats else "none", len(game_logs),
    )

    if is_pitcher:
        return score_pitcher(player, stats, game_logs, weights=weights, team_framing_runs=team_framing_runs)
    else:
        return score_batter(player, stats, game_logs, weights=weights)
