"""HV + TV hit-rate audit harness.

Replays the live env + leverage scoring stack on every slate in
data/historical_players.csv against actual is_highest_value AND
total_value outcomes.  Trait scoring is held constant (trait_factor =
1.0) because Statcast kinematics aren't in the historical CSV; this
isolates the env + leverage miscalibration the V15.x changelog
identified as the most likely HV-miss-rate causes.

The harness reports BOTH HV-rate and TV-rate metrics in a single pass.
TV (real_score × (slot_mult + card_boost), as recorded by the platform)
is the actual draft-win currency: a contrarian RS=4 with boost=3
produces TV=20, beating a star RS=8 with boost=0 (TV=16).  Tracking
both metrics simultaneously lets us see when calibration changes have
opposite directional effects on HV-rate vs TV-rate (the popularity
discount is the canonical example).

Outputs:
    stdout: corpus + per-slate HV-hit-rate@5/@10/@20 AND TV-rate@5/@10/@20
    scripts/output/hv_miss_decomposition.csv: every actual HV winner that
        ranked outside the top-5, with the per-multiplier deficits and a
        bucketed primary_miss_cause.

Per CLAUDE.md "calibration scripts in /scripts/ may read outcome columns
(real_score, total_value, is_highest_value, ...)" — this script does, but
it only writes to scripts/output/ and never touches app/.

CRITICAL: TV is treated strictly as an outcome label, identical to RS
and is_highest_value.  The runtime never reads any of these columns —
this script is output-only.  No boost predictor, no slot-ordering
heuristic that uses boost.  See app/services/filter_strategy.py for
the live runtime; this harness only measures it.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date as DateType
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")

from app.core import constants as _C  # noqa: E402
from app.core.constants import (  # noqa: E402
    MIN_SCORE_THRESHOLD,
    STACK_BONUS,
    canonicalize_team,
    is_stack_eligible_game,
)


def _get(name: str) -> float:
    """Read a constants attribute at call time so overrides via env vars are picked up."""
    return getattr(_C, name)


# ---- V16 Phase 2 trait-score reconstruction from CSV columns ----
#
# Mirrors app/services/scoring_engine.py FAITHFULLY on the sub-signals we
# have CSV data for.  All thresholds and sub-weights are read live from
# app.core.constants (single source of truth) so:
#   1. BO_OVERRIDE_<NAME> sweeps the SAME constant the live runtime uses
#   2. Drift between harness and live engine is impossible
#   3. Pre-V16-Phase-2 the harness had its own _TRAIT_* constants that
#      DIFFERED from the live thresholds (e.g. OPS floor 0.65 vs live
#      0.70).  Those duplicates are gone — the harness now scores against
#      the same gates the live engine uses.
#
# Skipped sub-signals (game-log dependent — harness can't reconstruct):
#   - score_batter_recent_form (game logs)
#   - score_hot_streak (game logs)
#   - score_speed_component (sb / games not in CSV)
#   - score_pitcher_recent_form (game logs)
#
# Re-normalisation:
#   batter total = offensive_profile / 40 × 100  (one of 4 traits, 40 of 100)
#   pitcher total = (ace_status + k_rate + era_whip) / 80 × 100  (3 of 4
#       pitcher traits, 80 of 100; recent_form skipped)


# Pitcher ace_status ERA buckets — kept inline as they're not currently
# sweep targets.  If you want to sweep the bucket boundaries, extract them
# to constants.py first.
_ACE_STATUS_BUCKETS = [
    (2.5, 1.00),
    (3.0, 0.85),
    (3.5, 0.70),
    (4.0, 0.50),
    (4.5, 0.30),
    (float("inf"), 0.10),
]


def _ace_status_score(era: float | None) -> float | None:
    """Mirror score_ace_status: ERA-bucketed quality multiplier on [0, 1]."""
    if era is None:
        return None
    for cutoff, frac in _ACE_STATUS_BUCKETS:
        if era < cutoff:
            return frac
    return _ACE_STATUS_BUCKETS[-1][1]


def _scale(value: float | None, floor: float, ceil: float) -> float | None:
    """Linear scale to [0, 1].  Ascending if floor<ceil, descending if floor>ceil."""
    if value is None:
        return None
    if floor < ceil:
        return max(0.0, min(1.0, (value - floor) / (ceil - floor)))
    return max(0.0, min(1.0, (floor - value) / (floor - ceil)))


def _weighted_normalize(components: list[tuple[float | None, float]]) -> float | None:
    """Weighted sum, dropping None subs from numerator AND denominator.

    Returns the [0, 1] normalised score, or None if no component had data.
    Mirrors the missing-component behaviour of score_offensive_profile.
    """
    num = sum(v * w for v, w in components if v is not None)
    den = sum(w for v, w in components if v is not None)
    if den == 0:
        return None
    return num / den


def _compute_offensive_profile_score(row: dict) -> float | None:
    """Mirror app.services.scoring_engine.score_offensive_profile.

    Returns 0-1 normalised score (None if OPS missing).  Live engine
    multiplies by max_pts=40; we re-normalise to 100 at the caller.
    """
    ops = _opt_float(row.get("ops_at_slate"))
    if ops is None:
        return None
    x_woba = _opt_float(row.get("x_woba"))
    hard_hit = _opt_float(row.get("hard_hit_pct"))
    barrel = _opt_float(row.get("barrel_pct"))
    avg_ev = _opt_float(row.get("avg_ev"))
    components = [
        (
            _scale(ops, _C.OFFENSIVE_PROFILE_OPS_FLOOR, _C.OFFENSIVE_PROFILE_OPS_CEILING),
            _C.OFFENSIVE_PROFILE_OPS_WEIGHT,
        ),
        (
            _scale(x_woba, _C.POWER_PROFILE_X_WOBA_FLOOR, _C.POWER_PROFILE_X_WOBA_CEILING),
            _C.OFFENSIVE_PROFILE_X_WOBA_WEIGHT,
        ),
        (
            _scale(hard_hit, _C.POWER_PROFILE_HARD_HIT_FLOOR, _C.POWER_PROFILE_HARD_HIT_MAX),
            _C.OFFENSIVE_PROFILE_HARD_HIT_WEIGHT,
        ),
        (
            _scale(barrel, _C.POWER_PROFILE_BARREL_PCT_FLOOR, _C.POWER_PROFILE_BARREL_PCT_MAX),
            _C.OFFENSIVE_PROFILE_BARREL_WEIGHT,
        ),
        (
            _scale(avg_ev, _C.POWER_PROFILE_AVG_EV_FLOOR, _C.POWER_PROFILE_AVG_EV_MAX),
            _C.OFFENSIVE_PROFILE_AVG_EV_WEIGHT,
        ),
    ]
    return _weighted_normalize(components)


def _compute_pitcher_k_rate_score(row: dict) -> float | None:
    """Mirror app.services.scoring_engine.score_pitcher_k_rate.

    Returns 0-1 normalised score.  When ≥3 kinematic sub-signals are
    present AND K/9 is also present, the result is a weighted blend
    (KIN_WEIGHT * kinematic + K9_WEIGHT * k9_norm).  Otherwise falls back
    to K/9 alone.
    """
    k9 = _opt_float(row.get("k9_at_slate"))
    fb_velo = _opt_float(row.get("fb_velo"))
    whiff = _opt_float(row.get("whiff_pct"))
    chase = _opt_float(row.get("chase_pct"))
    # Note: fb_ivb and fb_extension aren't in the historical CSV, so the
    # harness has at most 3 of the engine's 5 kinematic sub-signals.
    kin_subs = [
        _scale(fb_velo, _C.SCORING_FB_VELOCITY_FLOOR, _C.SCORING_FB_VELOCITY_CEILING),
        _scale(whiff, _C.SCORING_WHIFF_PCT_FLOOR, _C.SCORING_WHIFF_PCT_CEILING),
        _scale(chase, _C.SCORING_CHASE_PCT_FLOOR, _C.SCORING_CHASE_PCT_CEILING),
    ]
    kin_present = [v for v in kin_subs if v is not None]
    if len(kin_present) >= 3:
        kinematic = sum(kin_present) / len(kin_present)
        if k9 is not None:
            k9_norm = _scale(k9, _C.SCORING_K9_FLOOR, _C.SCORING_K9_CEILING)
            return (
                _C.KINEMATIC_BLEND_KIN_WEIGHT * kinematic + _C.KINEMATIC_BLEND_K9_WEIGHT * k9_norm
            )
        return kinematic
    # Fallback: K/9 alone.
    if k9 is None:
        return None
    return _scale(k9, _C.SCORING_K9_FLOOR, _C.SCORING_K9_CEILING)


def _compute_pitcher_era_whip_score(row: dict) -> float | None:
    """Mirror app.services.scoring_engine.score_pitcher_era_whip."""
    era = _opt_float(row.get("era_at_slate"))
    whip = _opt_float(row.get("whip_at_slate"))
    if era is None or whip is None:
        return None
    era_score = _scale(_C.SCORING_ERA_CEILING - era, 0.0, _C.SCORING_ERA_RANGE)
    whip_score = _scale(_C.SCORING_WHIP_CEILING - whip, 0.0, _C.SCORING_WHIP_RANGE)
    if era_score is None or whip_score is None:
        return None
    return era_score * _C.ERA_WHIP_ERA_WEIGHT + whip_score * _C.ERA_WHIP_WHIP_WEIGHT


def compute_trait_score_from_csv(row: dict, is_pitcher: bool) -> float | None:
    """Reconstruct a 0-100 trait_score from CSV columns.

    Faithful mirror of app.services.scoring_engine via the sub-scorers
    above.  Re-normalises to 0-100 across the engine traits the harness
    can compute (skipping recent_form / hot_streak / speed_component
    which all need game-log data not in the CSV).

    Engine weight budget covered:
      - Batter: offensive_profile (40 of 100)  → renormed to 100
      - Pitcher: ace_status + k_rate + era_whip (80 of 100) → renormed to 100

    Returns None if the row has no usable signal — caller falls back to
    neutral trait_factor (1.0), same as the live rookie path.
    """
    if is_pitcher:
        ace = _ace_status_score(_opt_float(row.get("era_at_slate")))
        krt = _compute_pitcher_k_rate_score(row)
        ewh = _compute_pitcher_era_whip_score(row)
        # Engine's pitcher trait sum (recent_form skipped) = 30+35+15 = 80.
        # Re-normalise to 100 at the end.
        wts = (
            (ace, _C.PITCHER_WEIGHT_ACE_STATUS),
            (krt, _C.PITCHER_WEIGHT_K_RATE),
            (ewh, _C.PITCHER_WEIGHT_ERA_WHIP),
        )
        norm = _weighted_normalize(list(wts))
        if norm is None:
            return None
        return norm * 100.0
    # Batter — only offensive_profile is reconstructable; the other three
    # batter traits (recent_form, hot_streak, speed) need game logs.
    norm = _compute_offensive_profile_score(row)
    if norm is None:
        return None
    return norm * 100.0


def trait_score_to_factor(trait_score: float) -> float:
    """Mirror filter_strategy._compute_base_ev's trait_factor formula.

    Reads TRAIT_MODIFIER_FLOOR / CEILING via _get() so BO_OVERRIDE_TRAIT_*
    sweeps are picked up at call time.
    """
    trait_floor_frac = MIN_SCORE_THRESHOLD / 100.0
    raw_trait = max(trait_score, float(MIN_SCORE_THRESHOLD)) / 100.0
    floor = _get("TRAIT_MODIFIER_FLOOR")
    ceiling = _get("TRAIT_MODIFIER_CEILING")
    if 1.0 - trait_floor_frac <= 0:
        return floor
    factor = floor + (raw_trait - trait_floor_frac) * (ceiling - floor) / (1.0 - trait_floor_frac)
    return max(floor, min(ceiling, factor))


from app.core.popularity import (  # noqa: E402
    popularity_score_to_multiplier,
    predict_popularity_score,
    predict_rookie_popularity_score,
)
from app.services.filter_strategy import (  # noqa: E402
    compute_batter_env_score,
    compute_pitcher_env_score,
)


PITCHER_POSITIONS = {"P", "SP", "RP"}
DEFAULT_BATTING_ORDER = 5  # mid-of-order; CSV has no batting_order column


def is_pitcher_pos(pos: str) -> bool:
    return (pos or "").strip().upper() in PITCHER_POSITIONS


def neutral_total_score() -> float:
    """Pick a total_score that yields trait_factor == 1.0.

    Solves _compute_base_ev's trait_factor formula for the pinned 1.0 point.
    Returns score in [0, 100] suitable to pass on a candidate.
    """
    trait_floor_frac = MIN_SCORE_THRESHOLD / 100.0
    floor = _get("TRAIT_MODIFIER_FLOOR")
    ceiling = _get("TRAIT_MODIFIER_CEILING")
    raw_trait = trait_floor_frac + (1.0 - floor) * (1.0 - trait_floor_frac) / (ceiling - floor)
    return raw_trait * 100.0


def load_slate_envs(path: Path) -> dict[str, dict[str, tuple[dict, bool]]]:
    """{date: {team_abbr: (game_dict, is_home)}} — every team mapped to its game.

    Both sides of the same game appear in the inner dict so the lookup is
    O(1) per player row.  is_home tells us which `home_*` / `away_*` fields
    to read for that team.
    """
    with path.open() as f:
        data = json.load(f)
    by_date: dict[str, dict[str, tuple[dict, bool]]] = {}
    for slate in data:
        date_str = slate["date"]
        team_to_game: dict[str, tuple[dict, bool]] = {}
        for g in slate.get("games", []):
            home = canonicalize_team(g["home"])
            away = canonicalize_team(g["away"])
            team_to_game[home] = (g, True)
            team_to_game[away] = (g, False)
        by_date[date_str] = team_to_game
    return by_date


def slate_stack_eligible_teams(team_to_game: dict[str, tuple[dict, bool]]) -> set[str]:
    """Compute which teams are stack-eligible on this slate (PATH 1/2/3)."""
    eligible: set[str] = set()
    seen_games: set[int] = set()
    for team, (game, is_home) in team_to_game.items():
        gid = id(game)
        if gid in seen_games:
            continue
        seen_games.add(gid)
        home = canonicalize_team(game["home"])
        away = canonicalize_team(game["away"])
        vt = game.get("vegas_total")
        home_ml = game.get("home_moneyline")
        away_ml = game.get("away_moneyline")
        home_starter_era = game.get("home_starter_era")
        away_starter_era = game.get("away_starter_era")
        home_team_ops = game.get("home_team_ops")
        away_team_ops = game.get("away_team_ops")

        # Path 2: extreme shootout — both sides eligible (no STACK_BONUS).
        # Skip path 2 here because PATH 1 carries the bonus and the harness
        # only uses the bonus dimension; mirror filter_strategy.py logic that
        # only PATH 1 favored teams flip is_in_blowout_game.
        # Evaluate each side for PATH 1 (blowout fav) and PATH 3 (catastrophic
        # opp SP) — these are the bonus-bearing paths.
        if (
            home_ml is not None
            and is_stack_eligible_game(home_ml, vt, away_starter_era, home_team_ops)
            and (
                (home_ml <= -200 and vt is not None and vt >= 9.0)
                or (
                    away_starter_era is not None
                    and home_team_ops is not None
                    and away_starter_era >= 6.5
                    and home_team_ops >= 0.760
                )
            )
        ):
            eligible.add(home)
        if (
            away_ml is not None
            and is_stack_eligible_game(away_ml, vt, home_starter_era, away_team_ops)
            and (
                (away_ml <= -200 and vt is not None and vt >= 9.0)
                or (
                    home_starter_era is not None
                    and away_team_ops is not None
                    and home_starter_era >= 6.5
                    and away_team_ops >= 0.760
                )
            )
        ):
            eligible.add(away)
    return eligible


def _opt_float(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    return float(s)


def score_one_player(
    row: dict,
    env_lookup: dict[str, tuple[dict, bool]],
    eligible: set[str],
    as_of: DateType,
    neutral_total: float,
) -> dict | None:
    """Compute filter_ev for a player row and return a record dict.

    Returns None if the player can't be scored (missing env, unknown team,
    etc.).  No fallbacks — same posture as the live pipeline.
    """
    team = canonicalize_team(row["team"])
    if team not in env_lookup:
        return None
    game, is_home = env_lookup[team]
    side = "home" if is_home else "away"
    other = "away" if is_home else "home"
    is_pitcher = is_pitcher_pos(row["position"])

    season_ops = _opt_float(row.get("ops_at_slate"))
    season_era = _opt_float(row.get("era_at_slate"))

    # ---- env score ----
    try:
        if is_pitcher:
            env_score, _factors = compute_pitcher_env_score(
                opp_team_ops=game.get(f"{other}_team_ops"),
                pitcher_k_per_9=game.get(f"{side}_starter_k_per_9"),
                park_team=game.get("park_team") or game["home"],
                is_home=is_home,
                team_moneyline=game.get(f"{side}_moneyline"),
                vegas_total=game.get("vegas_total"),
                own_starter_era=game.get(f"{side}_starter_era"),
            )
            batting_order: int | None = None
        else:
            env_score, _factors, _unk = compute_batter_env_score(
                opp_pitcher_era=game.get(f"{other}_starter_era"),
                opp_starter_whip=game.get(f"{other}_starter_whip"),
                park_team=game.get("park_team") or game["home"],
                team_moneyline=game.get(f"{side}_moneyline"),
                batting_order=DEFAULT_BATTING_ORDER,
                wind_speed_mph=game.get("wind_speed_mph"),
                wind_direction=game.get("wind_direction"),
                temperature_f=game.get("temperature_f"),
                platoon_advantage=False,  # CSV has no batter handedness column
            )
            batting_order = DEFAULT_BATTING_ORDER
    except Exception:
        return None

    # ---- popularity score ----
    is_rookie = season_era is None if is_pitcher else season_ops is None
    try:
        if is_rookie:
            pop_score = predict_rookie_popularity_score(
                player_name=row["player_name"],
                team=team,
                is_pitcher=is_pitcher,
                batting_order=batting_order,
                as_of=as_of,
            )
        elif is_pitcher:
            pop_score = predict_popularity_score(
                player_name=row["player_name"],
                team=team,
                is_pitcher=True,
                batting_order=None,
                season_ops=None,
                season_era=season_era,
                as_of=as_of,
            )
        else:
            pop_score = predict_popularity_score(
                player_name=row["player_name"],
                team=team,
                is_pitcher=False,
                batting_order=batting_order,
                season_ops=season_ops,
                season_era=None,
                as_of=as_of,
            )
    except Exception:
        return None

    # ---- assemble multipliers (mirrors _compute_base_ev) ----
    env_floor = _get("ENV_MODIFIER_FLOOR")
    if is_rookie:
        env_ceiling = _get("ROOKIE_ENV_MODIFIER_CEILING")
    elif is_pitcher:
        env_ceiling = _get("PITCHER_ENV_MODIFIER_CEILING")
    else:
        env_ceiling = _get("ENV_MODIFIER_CEILING")
    raw_env = max(env_score, 0.0)
    env_factor = env_floor + raw_env * (env_ceiling - env_floor)
    env_factor = max(env_floor, min(env_ceiling, env_factor))

    leverage_factor = popularity_score_to_multiplier(pop_score)

    in_blowout = (not is_pitcher) and (team in eligible)
    stack_bonus = STACK_BONUS if in_blowout else 1.0

    # V16 Phase 1: real trait_factor from Statcast + season aggregates in CSV.
    # Pre-V16 the harness held trait_factor = 1.0 because Statcast wasn't in
    # the CSV (V15.5 known limitation).  Phase 0 backfilled those columns;
    # this is the calibration-side payoff.  When the row has no usable
    # signals (true rookies, scraper name misses), trait_score is None and
    # we fall back to neutral 1.0 — same posture as the rookie-track in
    # the live runtime.
    trait_score = compute_trait_score_from_csv(row, is_pitcher)
    trait_factor = trait_score_to_factor(trait_score) if trait_score is not None else 1.0
    volatility_amp = 1.0
    dnp_adj = 1.0

    # V16 Phase 2: position_mult removed.  V13.3's catcher / 2B-SS haircut
    # was deleted in V16 Phase 1 (single source of truth: app/core/constants.py
    # no longer defines POSITION_VOLUME_MULTIPLIER).  Mirrors live runtime.
    filter_ev = (
        env_factor * volatility_amp * trait_factor * leverage_factor * stack_bonus * dnp_adj * 100.0
    )

    # tv is read strictly as an OUTCOME LABEL.  It is never an input to
    # filter_ev or to any model decision — the assignment below mirrors
    # the rs / is_hv reads, both of which are also outcome labels.
    rs_str = row.get("real_score") or ""
    tv_str = row.get("total_value") or ""
    try:
        rs_outcome = float(rs_str) if rs_str else 0.0
    except ValueError:
        rs_outcome = 0.0
    try:
        tv_outcome = float(tv_str) if tv_str else 0.0
    except ValueError:
        tv_outcome = 0.0

    return {
        "name": row["player_name"],
        "team": team,
        "position": row["position"],
        "is_pitcher": is_pitcher,
        "is_hv": int(row["is_highest_value"] or 0),
        "is_rookie": is_rookie,
        "filter_ev": filter_ev,
        "env_factor": env_factor,
        "leverage_factor": leverage_factor,
        "stack_bonus": stack_bonus,
        "trait_factor": trait_factor,
        "pop_score": pop_score,
        # outcome labels (NEVER inputs to scoring — see module docstring)
        "rs_outcome": rs_outcome,
        "tv_outcome": tv_outcome,
    }


def primary_miss_cause(c: dict) -> str:
    """Bucket the dominant downward multiplier for a missed HV winner."""
    deficits = {
        "low_env": max(0.0, 1.0 - c["env_factor"]),
        "leverage_discount": max(0.0, 1.0 - c["leverage_factor"]),
        "low_trait": max(0.0, 1.0 - c["trait_factor"]),
    }
    cause = max(deficits, key=lambda k: deficits[k])
    if deficits[cause] < 0.05:
        return "outranked"
    return cause


def _maybe_override(varname: str) -> None:
    """Allow CLI/env overrides for sweep mode.

    Patches both `app.core.constants` and any module that already imported
    the constant by name (popularity.py binds POPULARITY_* at import time).
    """
    env_key = f"BO_OVERRIDE_{varname}"
    val = os.environ.get(env_key)
    if val is None:
        return
    typed = type(getattr(_C, varname))(val)
    setattr(_C, varname, typed)
    # Patch downstream modules that bound this name at import time.
    from app.core import popularity as _pop

    if hasattr(_pop, varname):
        setattr(_pop, varname, typed)


SWEEPABLE_CONSTANTS = (
    # Env modifier band (per-position ceilings)
    "ENV_MODIFIER_FLOOR",
    "ENV_MODIFIER_CEILING",
    "PITCHER_ENV_MODIFIER_CEILING",
    "ROOKIE_ENV_MODIFIER_CEILING",
    # Popularity / leverage curve (V15.6, retuned V16 Phase 1)
    "POPULARITY_NEUTRAL_SCORE",
    "POPULARITY_SLOPE",
    "POPULARITY_MULT_FLOOR",
    "POPULARITY_MULT_CEILING",
    # Stack bonus (V13.3)
    "STACK_BONUS",
    # Trait modifier band (V15.4)
    "TRAIT_MODIFIER_FLOOR",
    "TRAIT_MODIFIER_CEILING",
    # V16 Phase 2 — trait sub-weights
    "OFFENSIVE_PROFILE_OPS_WEIGHT",
    "OFFENSIVE_PROFILE_X_WOBA_WEIGHT",
    "OFFENSIVE_PROFILE_HARD_HIT_WEIGHT",
    "OFFENSIVE_PROFILE_BARREL_WEIGHT",
    "OFFENSIVE_PROFILE_AVG_EV_WEIGHT",
    "KINEMATIC_BLEND_KIN_WEIGHT",
    "KINEMATIC_BLEND_K9_WEIGHT",
    "ERA_WHIP_ERA_WEIGHT",
    "ERA_WHIP_WHIP_WEIGHT",
    "PITCHER_WEIGHT_ACE_STATUS",
    "PITCHER_WEIGHT_K_RATE",
    "PITCHER_WEIGHT_ERA_WHIP",
    "BATTER_WEIGHT_OFFENSIVE_PROFILE",
    # Composition caps
    "MAX_PLAYERS_PER_TEAM_BATTERS_STACKABLE",
    "MAX_PLAYERS_PER_GAME_BATTERS",
)


def apply_sweep_overrides() -> None:
    """Read every BO_OVERRIDE_<name> env var and patch app.core.constants.

    Single source of truth for which constants are sweepable.  Both
    audit_hv_hit_rate.py and audit_lineup_tv.py call this so the override
    surface is consistent across harnesses.
    """
    for v in SWEEPABLE_CONSTANTS:
        _maybe_override(v)


def main() -> int:
    apply_sweep_overrides()

    historical_csv = ROOT / "data" / "historical_players.csv"
    slate_results_json = ROOT / "data" / "historical_slate_results.json"
    output_dir = ROOT / "scripts" / "output"
    output_dir.mkdir(exist_ok=True)
    output_csv = output_dir / "hv_miss_decomposition.csv"

    slate_envs = load_slate_envs(slate_results_json)

    rows_by_date: dict[str, list[dict]] = defaultdict(list)
    with historical_csv.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows_by_date[row["date"]].append(row)

    NEUTRAL_TOTAL = neutral_total_score()

    per_slate: list[tuple] = []
    miss_rows: list[dict] = []
    skipped_total = 0

    for date_str in sorted(rows_by_date):
        if date_str not in slate_envs:
            continue
        env_lookup = slate_envs[date_str]
        eligible = slate_stack_eligible_teams(env_lookup)
        as_of = DateType.fromisoformat(date_str)

        scored: list[dict] = []
        skipped = 0
        for row in rows_by_date[date_str]:
            rec = score_one_player(row, env_lookup, eligible, as_of, NEUTRAL_TOTAL)
            if rec is None:
                skipped += 1
                continue
            scored.append(rec)
        skipped_total += skipped

        scored.sort(key=lambda c: c["filter_ev"], reverse=True)
        for rank, c in enumerate(scored, start=1):
            c["rank"] = rank

        # Per-slate TV ranking — computed once and attached to each record
        # so the miss CSV can flag "missed top-K-TV" as well as "missed HV".
        for tv_rank, c in enumerate(sorted(scored, key=lambda x: -x["tv_outcome"]), start=1):
            c["tv_rank"] = tv_rank
        # Top-K-TV reference set for the per-slate hit-rate
        top5_tv = {(c["name"], c["team"]) for c in scored if c["tv_rank"] <= 5}
        top10_tv = {(c["name"], c["team"]) for c in scored if c["tv_rank"] <= 10}
        top20_tv = {(c["name"], c["team"]) for c in scored if c["tv_rank"] <= 20}

        total_hv = sum(c["is_hv"] for c in scored)
        if total_hv == 0:
            continue
        hv5 = sum(c["is_hv"] for c in scored[:5])
        hv10 = sum(c["is_hv"] for c in scored[:10])
        hv20 = sum(c["is_hv"] for c in scored[:20])
        # TV-rate metrics: how many of our top-K filter_ev picks are
        # also in the slate's top-K by TV outcome?
        tv5 = sum(1 for c in scored[:5] if (c["name"], c["team"]) in top5_tv)
        tv10 = sum(1 for c in scored[:10] if (c["name"], c["team"]) in top10_tv)
        tv20 = sum(1 for c in scored[:20] if (c["name"], c["team"]) in top20_tv)
        # Slot-1 TV: was our #1 filter_ev pick also a top-K-TV winner?
        slot1_in_top5_tv = 1 if scored and scored[0]["tv_rank"] <= 5 else 0
        slot1_tv_outcome = scored[0]["tv_outcome"] if scored else 0.0
        per_slate.append(
            (
                date_str,
                hv5,
                hv10,
                hv20,
                total_hv,
                len(scored),
                skipped,
                tv5,
                tv10,
                tv20,
                slot1_in_top5_tv,
                slot1_tv_outcome,
            )
        )

        for c in scored:
            if c["is_hv"] == 1 and c["rank"] > 5:
                miss_rows.append(
                    {
                        "date": date_str,
                        "name": c["name"],
                        "team": c["team"],
                        "position": c["position"],
                        "is_pitcher": int(c["is_pitcher"]),
                        "is_rookie": int(c["is_rookie"]),
                        "rank": c["rank"],
                        "filter_ev": round(c["filter_ev"], 2),
                        "env_factor": round(c["env_factor"], 3),
                        "leverage_factor": round(c["leverage_factor"], 3),
                        "stack_bonus": round(c["stack_bonus"], 3),
                        "trait_factor": round(c["trait_factor"], 3),
                        "pop_score": round(c["pop_score"], 2),
                        "primary_miss_cause": primary_miss_cause(c),
                    }
                )

    with output_csv.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "date",
                "name",
                "team",
                "position",
                "is_pitcher",
                "is_rookie",
                "rank",
                "filter_ev",
                "env_factor",
                "leverage_factor",
                "stack_bonus",
                "trait_factor",
                "pop_score",
                "primary_miss_cause",
            ],
        )
        writer.writeheader()
        writer.writerows(miss_rows)

    n_slates = len(per_slate)
    if n_slates == 0:
        print("No slates scored — check historical_slate_results.json coverage.")
        return 1

    total_hv = sum(r[4] for r in per_slate)
    total_at_5 = sum(r[1] for r in per_slate)
    total_at_10 = sum(r[2] for r in per_slate)
    total_at_20 = sum(r[3] for r in per_slate)
    total_pool = sum(r[5] for r in per_slate)
    # TV-rate aggregates
    total_tv5 = sum(r[7] for r in per_slate)
    total_tv10 = sum(r[8] for r in per_slate)
    total_tv20 = sum(r[9] for r in per_slate)
    slot1_top5_tv = sum(r[10] for r in per_slate)
    slot1_tv_total = sum(r[11] for r in per_slate)

    print(f"Slates scored:    {n_slates}")
    print(f"Players ranked:   {total_pool}  (skipped {skipped_total})")
    print(f"HV winners total: {total_hv}")
    print()
    print("=== HV-rate (binary leaderboard hit) ===")
    print(
        f"HV captured @5:   {total_at_5} / {total_hv} ({total_at_5 / total_hv:.1%})  avg/slate {total_at_5 / n_slates:.2f}"
    )
    print(
        f"HV captured @10:  {total_at_10} / {total_hv} ({total_at_10 / total_hv:.1%})  avg/slate {total_at_10 / n_slates:.2f}"
    )
    print(
        f"HV captured @20:  {total_at_20} / {total_hv} ({total_at_20 / total_hv:.1%})  avg/slate {total_at_20 / n_slates:.2f}"
    )
    print()
    # TV-rate denominators are slate-bounded: each slate has exactly K
    # top-K-TV winners, so the corpus-level cap is K * n_slates.
    cap5 = 5 * n_slates
    cap10 = 10 * n_slates
    cap20 = 20 * n_slates
    print("=== TV-rate (top-K by total_value, the actual draft-win currency) ===")
    print(
        f"TV captured @5:   {total_tv5} / {cap5} ({total_tv5 / cap5:.1%})  avg/slate {total_tv5 / n_slates:.2f}"
    )
    print(
        f"TV captured @10:  {total_tv10} / {cap10} ({total_tv10 / cap10:.1%})  avg/slate {total_tv10 / n_slates:.2f}"
    )
    print(
        f"TV captured @20:  {total_tv20} / {cap20} ({total_tv20 / cap20:.1%})  avg/slate {total_tv20 / n_slates:.2f}"
    )
    print(f"Slot-1 in top-5 TV:  {slot1_top5_tv}/{n_slates} ({slot1_top5_tv / n_slates:.1%})")
    print(f"Mean slot-1 TV outcome: {slot1_tv_total / n_slates:.2f}")
    print()

    cause_counts: dict[str, int] = defaultdict(int)
    pitcher_cause: dict[str, int] = defaultdict(int)
    batter_cause: dict[str, int] = defaultdict(int)
    for m in miss_rows:
        cause_counts[m["primary_miss_cause"]] += 1
        if m["is_pitcher"]:
            pitcher_cause[m["primary_miss_cause"]] += 1
        else:
            batter_cause[m["primary_miss_cause"]] += 1

    print(f"Miss decomposition (HV winners outside top-5, n={len(miss_rows)}):")
    for cause, count in sorted(cause_counts.items(), key=lambda x: -x[1]):
        p = pitcher_cause.get(cause, 0)
        b = batter_cause.get(cause, 0)
        print(f"  {cause:22s} {count:4d}  (pitcher={p}, batter={b})")
    print()
    print(f"Decomposition CSV: {output_csv}")
    print("Calibration constants used:")
    print(
        f"  ENV_FLOOR / batter_CEIL / pitcher_CEIL / rookie_CEIL = {_get('ENV_MODIFIER_FLOOR')} / {_get('ENV_MODIFIER_CEILING')} / {_get('PITCHER_ENV_MODIFIER_CEILING')} / {_get('ROOKIE_ENV_MODIFIER_CEILING')}"
    )
    print(
        f"  POPULARITY: NEUTRAL={_get('POPULARITY_NEUTRAL_SCORE')}, SLOPE={_get('POPULARITY_SLOPE')}, mult range [{_get('POPULARITY_MULT_FLOOR')}, {_get('POPULARITY_MULT_CEILING')}]"
    )
    print(
        f"  TRAIT band [{_get('TRAIT_MODIFIER_FLOOR')}, {_get('TRAIT_MODIFIER_CEILING')}]  (V16 Phase 1: real Statcast-driven trait_factor)"
    )
    print(
        f"  Trait sub-weights — OPS={_get('OFFENSIVE_PROFILE_OPS_WEIGHT')} "
        f"xwOBA={_get('OFFENSIVE_PROFILE_X_WOBA_WEIGHT')} "
        f"HH%={_get('OFFENSIVE_PROFILE_HARD_HIT_WEIGHT')} "
        f"brl%={_get('OFFENSIVE_PROFILE_BARREL_WEIGHT')} "
        f"EV={_get('OFFENSIVE_PROFILE_AVG_EV_WEIGHT')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
