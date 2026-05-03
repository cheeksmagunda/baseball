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
    PARK_HR_FACTOR_MAX,
    PARK_HR_FACTOR_MIN,
    PARK_HR_FACTORS,
    PITCHER_POSITIONS,
    POWER_PROFILE_AVG_EV_MAX,
    POWER_PROFILE_BARREL_PCT_MAX,
    POWER_PROFILE_HARD_HIT_MAX,
    POWER_PROFILE_MAX_EV_CEILING,
    POWER_PROFILE_X_WOBA_CEILING,
    POWER_PROFILE_X_WOBA_FLOOR,
    SCORING_BATTER_ERA_FLOOR,
    SCORING_BATTER_ERA_RANGE,
    SCORING_BATTER_K_PCT_CEILING,
    SCORING_BATTER_K_PCT_FLOOR,
    SCORING_BATTER_OPS_SPLIT_FLOOR,
    SCORING_BATTER_OPS_SPLIT_RANGE,
    SCORING_BATTER_WHIP_FLOOR,
    SCORING_BATTER_WHIP_RANGE,
    SCORING_OPP_K9_VULN_CEILING,
    SCORING_OPP_K9_VULN_FLOOR,
    SCORING_OPP_X_WOBA_AGAINST_CEILING,
    SCORING_OPP_X_WOBA_AGAINST_FLOOR,
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
    SCORING_PITCHER_K_PCT_FLOOR,
    SCORING_PITCHER_K_PCT_RANGE,
    SCORING_PITCHER_OPS_CEILING,
    SCORING_PITCHER_OPS_RANGE,
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


def score_pitcher_matchup(
    opp_team: str | None, opp_stats: dict | None, max_pts: float
) -> TraitResult:
    """Score based on opponent offensive quality. Weaker opponent = higher score."""
    if max_pts == 0:
        return TraitResult("matchup_quality", 0.0, 0.0, "weight=0")

    if not opp_team or not opp_stats:
        raise RuntimeError(
            f"score_pitcher_matchup called with opp_team={opp_team!r}, opp_stats={opp_stats} "
            "— upstream pipeline should have raised before scoring"
        )

    opp_ops = opp_stats["ops"]
    opp_k_pct = opp_stats["k_pct"]

    # OPS component: lower is better for pitcher (inverted scale)
    ops_score = scale_score(SCORING_PITCHER_OPS_CEILING - opp_ops, 0, SCORING_PITCHER_OPS_RANGE, 1.0)
    # K% component: higher K% is better for pitcher
    k_score = scale_score(opp_k_pct - SCORING_PITCHER_K_PCT_FLOOR, 0, SCORING_PITCHER_K_PCT_RANGE, 1.0)

    combined = (ops_score * 0.6 + k_score * 0.4) * max_pts
    return TraitResult(
        "matchup_quality",
        round(combined, 1),
        max_pts,
        f"vs {opp_ops:.3f} OPS | {opp_k_pct:.1%} K-rate",
    )


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

def score_power_profile(stats: PlayerStats | None, max_pts: float) -> TraitResult:
    """Score power based on Statcast kinematics + HR rate.

    V10.0: rebuilt around what the target app actually rewards — distance of
    the play.  Strategy doc §"Offensive Engine": avg exit velocity, hard-hit
    %, and barrel % are the upstream signals that produce 400+ ft home runs
    and 105 mph hits.  HR/PA and max EV are kept as thin confirmation
    signals (2 pts each) — they lag the physical profile.

    V10.8 components (25-pt denominator):
      avg_exit_velocity  → 7 pts  (92 mph floor, league-avg power)
      hard_hit_pct       → 7 pts  (50% → elite sluggers)
      barrel_pct         → 6 pts  (15% → Stewart/DeLauter tier)
      x_woba             → 4 pts  (NEW V10.8 — Statcast xwOBA, contact-quality leading indicator)
      max_exit_velocity  → 1 pt   (V10.8: trimmed from 2 → 1, x_woba absorbs power-tail confirmation)
      HR/PA              → 0 pts  (V10.8: removed — MLB API never populated reliably; lagging outcome anyway)

    The 25-pt total is preserved so the trait's contribution to total_score
    stays comparable across versions and existing tests.

    Strict policy (May 2026): a batter with zero PA, or whose Statcast row
    has every signal None, is not scoreable from live data and is excluded
    by the DNP filter upstream.  Reaching this function with PA=0 or a fully
    empty Statcast row raises — the pool must not contain rookies with no
    measurable MLB performance.
    """
    if not stats or stats.pa == 0:
        raise RuntimeError(
            "power_profile called with no stats or 0 PA for batter — "
            "upstream filter should have excluded this player"
        )

    avg_ev = stats.avg_exit_velocity
    hard_hit = stats.hard_hit_pct
    barrel_pct = stats.barrel_pct
    max_ev = stats.max_exit_velocity
    x_woba = stats.x_woba   # V10.8 — Savant xwOBA, contact-quality leading indicator

    # Each sub-score is 0.0–1.0, then weighted by its point allotment.
    avg_ev_score = scale_score(avg_ev, 85.0, POWER_PROFILE_AVG_EV_MAX, 1.0) if avg_ev is not None else None
    hard_hit_score = scale_score(hard_hit, 30.0, POWER_PROFILE_HARD_HIT_MAX, 1.0) if hard_hit is not None else None
    barrel_score = scale_score(barrel_pct, 4.0, POWER_PROFILE_BARREL_PCT_MAX, 1.0) if barrel_pct is not None else None
    max_ev_score = scale_score(max_ev, 105.0, POWER_PROFILE_MAX_EV_CEILING, 1.0) if max_ev is not None else None
    x_woba_score = scale_score(x_woba, POWER_PROFILE_X_WOBA_FLOOR, POWER_PROFILE_X_WOBA_CEILING, 1.0) if x_woba is not None else None

    # Weighted sum. Missing sub-scores drop out of the numerator AND denominator
    # so rookies with partial Statcast coverage aren't penalised for sparse data.
    # V10.8 weights: avg_ev 7 + hard_hit 7 + barrel 6 + x_woba 4 + max_ev 1 = 25.
    components = [
        (avg_ev_score, 7.0, "EV"),
        (hard_hit_score, 7.0, "HH%"),
        (barrel_score, 6.0, "brl%"),
        (x_woba_score, 4.0, "xwOBA"),
        (max_ev_score, 1.0, "maxEV"),
    ]
    weighted_sum = sum(s * w for s, w, _ in components if s is not None)
    denom = sum(w for s, w, _ in components if s is not None)
    if denom == 0:
        raise RuntimeError(
            "power_profile: batter has stats but every Statcast signal is None "
            "(avg_ev/hard_hit/barrel/x_woba/max_ev) — upstream DNP filter "
            "should have excluded this player"
        )

    # Scale the realised fraction up to the full POWER_PROFILE_DENOM (25) so a
    # partial-data batter can still saturate max_pts if their present signals
    # all max out — but not arbitrarily: the denominator reflects evidence.
    total = (weighted_sum / denom) * max_pts

    detail_parts = []
    if avg_ev is not None:
        detail_parts.append(f"{avg_ev:.1f} avg EV")
    if hard_hit is not None:
        detail_parts.append(f"{hard_hit:.0f}% hard-hit")
    if barrel_pct is not None:
        detail_parts.append(f"{barrel_pct:.0f}% barrel")
    if x_woba is not None:
        detail_parts.append(f"{x_woba:.3f} xwOBA")
    if max_ev is not None:
        detail_parts.append(f"{max_ev:.1f} max EV")

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
    if max_pts == 0:
        return TraitResult("lineup_position", 0.0, 0.0, "weight=0")

    if batting_order is None:
        raise RuntimeError(
            "lineup_position called with batting_order=None — upstream DNP "
            "filter should have excluded batters not in the projected lineup"
        )

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
    """Score matchup vs opposing starter.  Higher opponent ERA = better for batter.

    Sub-signals (blended; weights re-normalised when one is missing):
      * pitcher ERA       — opp ERA above 2.5 produces credit (35% weight).
      * pitcher WHIP      — opp WHIP above 0.9 produces credit (20% weight).
      * hand-split OPS    — batter's season OPS vs starter handedness, when
                            both starter_hand and batter_hand are known and
                            batter is not switch (30% weight).
      * K-vulnerability   — V10.6: cross-axis penalty.  Batter K% × opp K/9
                            crossed; full penalty fires only when BOTH are
                            high (high-K batter vs elite K-pitcher = 0-fer
                            floor risk).  Contact hitter or contact pitcher
                            individually = no penalty.  15% weight, applied
                            as `(1 - vuln) * weight` so the credit is
                            preserved on safe matchups.

    Falls back to ERA/WHIP-only when no other signal is available.  Switch
    hitters (bat_side = "S") skip the hand-split — they don't carry a
    single split.
    """
    # V12.2 zero-weighted in production (env handles opp ERA/WHIP).  When
    # max_pts is 0 the trait contributes nothing to the total, so skip the
    # data-presence check — there is no scoring to do.
    if max_pts == 0:
        return TraitResult("matchup_quality", 0.0, 0.0, "weight=0")

    if not opp_pitcher_stats:
        raise RuntimeError(
            "score_batter_matchup: opp_pitcher_stats is None — caller must "
            "supply opposing-starter ERA/WHIP from the live SlateGame row"
        )

    opp_era = opp_pitcher_stats.get("era")
    opp_whip = opp_pitcher_stats.get("whip")
    if opp_era is None or opp_whip is None:
        raise RuntimeError(
            f"score_batter_matchup: opp ERA={opp_era}, WHIP={opp_whip} — "
            "data collection should populate both from the live MLB API"
        )

    # Opponent ERA: higher is better for batter
    era_score = scale_score(opp_era - SCORING_BATTER_ERA_FLOOR, 0, SCORING_BATTER_ERA_RANGE, 1.0)
    # Opponent WHIP: higher is better for batter
    whip_score = scale_score(opp_whip - SCORING_BATTER_WHIP_FLOOR, 0, SCORING_BATTER_WHIP_RANGE, 1.0)

    detail = f"vs ERA {opp_era:.2f} / WHIP {opp_whip:.2f}"

    # Handedness-specific OPS split: direct conditional sensitivity signal.
    # Uses the batter's actual season OPS vs this pitcher handedness; falls back
    # to league-average default when splits are absent.  Switch hitters (S) are
    # skipped — they optimally face both hands and don't carry a single split.
    ops_split_score = None
    if starter_hand and batter_hand and batter_hand != "S":
        if starter_hand == "L":
            batter_ops = batter_stats.ops_vs_lhp if batter_stats else None
            if batter_ops is not None:
                ops_split_score = scale_score(batter_ops - SCORING_BATTER_OPS_SPLIT_FLOOR, 0, SCORING_BATTER_OPS_SPLIT_RANGE, 1.0)
                detail += f" | {batter_ops:.3f} OPS vs LHP"
        elif starter_hand == "R":
            batter_ops = batter_stats.ops_vs_rhp if batter_stats else None
            if batter_ops is not None:
                ops_split_score = scale_score(batter_ops - SCORING_BATTER_OPS_SPLIT_FLOOR, 0, SCORING_BATTER_OPS_SPLIT_RANGE, 1.0)
                detail += f" | {batter_ops:.3f} OPS vs RHP"

    # V10.6: K-vulnerability cross-axis sub-signal.
    # Computes 0..1 via batter_k_pct × opp_k9 (both normalised), inverted to a
    # credit so safe matchups (low batter K% OR low opp K/9) preserve max
    # contribution while only the cross (high × high) is penalised.
    # Both stats must be present to evaluate; falls through to None otherwise.
    k_vuln_credit = None
    opp_k9 = opp_pitcher_stats.get("k_per_9")
    if (
        opp_k9 is not None
        and batter_stats is not None
        and batter_stats.pa is not None
        and batter_stats.pa > 0
        and batter_stats.so is not None
    ):
        batter_k_pct = batter_stats.so / max(batter_stats.pa, 1)
        bk_norm = scale_score(
            batter_k_pct - SCORING_BATTER_K_PCT_FLOOR,
            0,
            SCORING_BATTER_K_PCT_CEILING - SCORING_BATTER_K_PCT_FLOOR,
            1.0,
        )
        opp_k9_norm = scale_score(
            opp_k9 - SCORING_OPP_K9_VULN_FLOOR,
            0,
            SCORING_OPP_K9_VULN_CEILING - SCORING_OPP_K9_VULN_FLOOR,
            1.0,
        )
        # Cross-axis: only the (high × high) corner fires the full penalty.
        vuln = bk_norm * opp_k9_norm
        k_vuln_credit = 1.0 - vuln
        if vuln >= 0.30:
            detail += f" | K-vuln {vuln:.0%} (batter K%={batter_k_pct:.0%}, opp K/9={opp_k9:.1f})"

    # V10.8: opposing-arsenal-effectiveness sub-signal via xwOBA-against.
    # Independent of ERA/WHIP — those are sequencing-sensitive outcomes;
    # xwOBA-against is the leading indicator of contact quality the arsenal
    # is allowing.  Inverted scale: lower xwOBA-against = elite arsenal =
    # WORSE for the batter's matchup, hence the (1 − x_woba_credit) inverted
    # contribution.  Skipped when the opposing pitcher has no Savant row
    # yet (rookie pre-50 PA).  10% weight when present — meaningful without
    # double-counting ERA which already captures the realised version of
    # this signal.
    arsenal_credit = None
    opp_x_woba_against = opp_pitcher_stats.get("x_woba_against")
    if opp_x_woba_against is not None:
        # graduated_scale arg order — descending range (floor > ceiling).
        norm = scale_score(
            SCORING_OPP_X_WOBA_AGAINST_FLOOR - opp_x_woba_against,
            0,
            SCORING_OPP_X_WOBA_AGAINST_FLOOR - SCORING_OPP_X_WOBA_AGAINST_CEILING,
            1.0,
        )
        # `norm` is 0 when opp xwOBA-against ≥ floor (weak arsenal, batter
        # favored), 1 when ≤ ceiling (elite arsenal, batter suppressed).
        # The matchup credit is the inverse — high norm = low credit.
        arsenal_credit = 1.0 - norm
        if norm >= 0.30:
            detail += f" | arsenal xwOBA-against {opp_x_woba_against:.3f}"

    # Blend.  Weight rebalancing depends on which sub-signals are available.
    # V10.8 default (5 signals): era 30% + whip 18% + split 27% + k-vuln 15% + arsenal 10%.
    if ops_split_score is not None and k_vuln_credit is not None and arsenal_credit is not None:
        combined = (
            era_score * 0.30
            + whip_score * 0.18
            + ops_split_score * 0.27
            + k_vuln_credit * 0.15
            + arsenal_credit * 0.10
        ) * max_pts
    elif ops_split_score is not None and k_vuln_credit is not None:
        # 4 signals (no arsenal): the V10.6 blend.
        combined = (
            era_score * 0.35
            + whip_score * 0.20
            + ops_split_score * 0.30
            + k_vuln_credit * 0.15
        ) * max_pts
    elif ops_split_score is not None:
        # ERA + WHIP + split (no K-vuln, e.g., rookie batter with 0 PA so far).
        combined = (era_score * 0.40 + whip_score * 0.25 + ops_split_score * 0.35) * max_pts
    elif k_vuln_credit is not None:
        # ERA + WHIP + K-vuln (no hand split, e.g., switch hitter).
        combined = (
            era_score * 0.50
            + whip_score * 0.30
            + k_vuln_credit * 0.20
        ) * max_pts
    else:
        # ERA + WHIP only — bare-bones matchup with no additional signals.
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
    if max_pts == 0:
        return TraitResult("ballpark_factor", 0.0, 0.0, "weight=0")

    if not park_team:
        raise RuntimeError(
            "ballpark_factor called without park_team — every SlateGame has a "
            "home_team, so a None here is a data integrity error"
        )

    if park_team not in PARK_HR_FACTORS:
        raise RuntimeError(
            f"ballpark_factor: park_team={park_team!r} not in PARK_HR_FACTORS — "
            "team abbreviation must match the canonical 30-team set"
        )
    base_factor = PARK_HR_FACTORS[park_team]
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

def score_pitcher(
    player: Player,
    stats: PlayerStats | None,
    game_logs: list[PlayerGameLog],
    opp_team: str | None = None,
    opp_team_stats: dict | None = None,
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
    team_framing_runs: float | None = None,
) -> PlayerScoreResult:
    """Score any player (auto-detects pitcher vs batter, override with is_pitcher).

    The is_pitcher override is required for two-way players (e.g. Ohtani) whose
    DB position is 'DH' but who are confirmed starters for today's game.
    Without the override, score_player routes them to score_batter, producing
    batter traits (power_profile, etc.) while the caller expects pitcher traits
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
        logger.debug(
            "score_player: no season-%d stats for %s (%s) — using defaults",
            settings.current_season, player.name, player.team,
        )

    # Caller override takes precedence; fall back to DB position.
    if is_pitcher is None:
        is_pitcher = player.position in PITCHER_POSITIONS

    logger.debug(
        "scoring %s (%s, %s) as_pitcher=%s stats=%s game_logs=%d",
        player.name, player.team, player.position, is_pitcher,
        "yes" if stats else "none", len(game_logs),
    )

    if is_pitcher:
        return score_pitcher(
            player, stats, game_logs,
            opp_team=opp_team,
            opp_team_stats=opp_team_stats,
            weights=weights,
            team_framing_runs=team_framing_runs,
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
