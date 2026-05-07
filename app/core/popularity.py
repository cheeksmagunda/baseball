"""Predicted-ownership popularity score (V15.1, May 2026).

The pipeline is, by construction, a performance predictor.  The 40-slate
audit (STRATEGY_AUDIT_2026-05.md) shows that performance prediction alone
cannot win Real Sports daily drafts: 92.5% of historical winning lineups
contain at least one Highest Value player who was not on the Most Popular
leaderboard, and popular HVs vs sleeper HVs score essentially identically.
The contest-winning edge is differentiation from the field, not raw
projection.

V15 (May 2026) replaced V14's discrete-bucket leverage system with a
continuous popularity score in [0, ~10] mapping to a continuous EV
multiplier in [POPULARITY_MULT_FLOOR, POPULARITY_MULT_CEILING] via
popularity_score_to_multiplier (calibrated by
scripts/calibrate_popularity_curve.py against MP-flag outcomes).

V15.1 (May 2026, this revision) replaces the binary thresholds INSIDE the
score with continuous functions fit against the same outcome data:

  * Elite stats — V15 used OPS >= 0.900 → +2 / ERA <= 3.00 → +2 (binary).
    V15.1 uses a smooth ramp: OPS in [0.650, 0.950] → [0, 2.5] pts;
    ERA in [2.50, 4.50] → [2.5, 0] pts.  The bucket analysis shows the
    actual MP-rate ramp starts at OPS 0.65 (7% MP) and saturates by 0.95
    (64% MP) — V15's threshold collapsed this gradient into a single +2
    cliff and treated everyone outside as identical.

  * Fame index — V15 used fame_count >= 1 → +1 / >= 3 → +2 (binary,
    14-day window).  V15.1 uses a rate-based signal:
    mp_appearances / total_appearances over the trailing window, scaled
    to [0, 3] pts.  Position-aware window: 28 days for pitchers (5-6
    starts as denominator), 14 days for batters (~10 appearances).
    Calibration shows the rate signal lifts batter MP-prediction AUC
    0.823 → 0.848 by separating "popular every start" from "popular
    once per fortnight" — V15 collapsed both to +1.

  * Market tier and STAR_PLAYER_FLAGS retained — both showed clean
    monotonic signal in calibration (tier 1 = 55% MP-rate vs tier 4 =
    34% MP-rate; flagged stars 66% vs unflagged 38%).

  * Top-3 batting order — retained as binary +1.  Not in the historical
    CSV, so cannot be fit against MP-flag outcomes.  Live runtime keeps
    the binary bonus.

Inputs that are FORBIDDEN by the architecture and not consumed here:
    - `card_boost` (revealed only during/after draft)
    - `drafts` (raw historical count is an outcome label)
    - `real_score`, `total_value`, `is_highest_value` (post-game truth)

Rookie interaction: rookie-track players (no fame, no stats, low team
score on small markets) naturally land near score 0–3 and earn a positive
multiplier (≥1.0).  The V13.3 env cap (ROOKIE_ENV_MODIFIER_CEILING = 1.10)
still constrains them on the env side, so a rookie pitcher's net EV
ceiling stays well below a veteran's — but the popularity boost keeps
them competitive in genuinely strong env contexts rather than getting
double-faded.

The audit script (scripts/audit_live_isolation.py) exempts this module
from the no-historical-bleed rule because the only data file it touches
is data/historical_players.csv, and only the prior-slate is_most_popular
flag — a publicly-visible pre-game observable for any future slate.  The
read is bounded to dates strictly before the current slate.
"""

from __future__ import annotations

import csv
import unicodedata
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from app.core.constants import (
    LEVERAGE_ELITE_BATTER_OPS_CEILING,
    LEVERAGE_ELITE_BATTER_OPS_FLOOR,
    LEVERAGE_ELITE_PITCHER_ERA_CEILING,
    LEVERAGE_ELITE_PITCHER_ERA_FLOOR,
    LEVERAGE_ELITE_STAT_MAX_PTS,
    LEVERAGE_FAME_INDEX_DAYS_BATTER,
    LEVERAGE_FAME_INDEX_DAYS_PITCHER,
    LEVERAGE_FAME_RATE_MAX_PTS,
    POPULARITY_MULT_CEILING,
    POPULARITY_MULT_FLOOR,
    POPULARITY_NEUTRAL_SCORE,
    POPULARITY_SLOPE,
    STAR_PLAYER_FLAGS,
    TEAM_MARKET_TIER,
    canonicalize_team,
)


# Path to the prior-slate fame source.  Same file as the calibration
# corpus, but only its date + player_name + team + is_most_popular columns
# are consumed here, and only for dates strictly before the current slate.
_FAME_SOURCE = Path(__file__).resolve().parents[2] / "data" / "historical_players.csv"


def _normalize(name: str) -> str:
    """Same normalization as app.models.player.normalize_name.

    Local copy so this module has no SQLAlchemy import dependency — it can
    be called from non-DB contexts (offline calibration, tests).
    """
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(ascii_name.lower().split())


@lru_cache(maxsize=8)
def _load_fame_rate_index(
    as_of: date,
    window_days: int,
) -> dict[tuple[str, str], tuple[int, int]]:
    """Build {(name_normalized, team): (mp_appearances, total_appearances)}.

    Cached per (as_of, window_days) pair — a single T-65 pipeline run
    scores ~250 candidates split between pitchers (28-day window) and
    batters (14-day window), and would otherwise re-read the CSV up to
    that many times.

    Both the numerator (MP appearances) and denominator (total
    appearances) are scoped to the trailing `window_days` strictly before
    `as_of`.  The current-slate row, even if present in the CSV ahead of
    time (it is not), would be excluded — the function does not see
    today's outcome.

    The denominator captures "any appearance in the leaderboard corpus"
    — MP, HV, or 3X.  This is the right denominator for a "given the
    field considered drafting you, how often did they make you popular"
    rate; using it lets us distinguish a pitcher MP'd 1 of 2 starts (50%
    rate) from one MP'd 2 of 2 starts (100% rate), which V15's binary
    fame_count >= 1 collapsed to identical +1 contributions.
    """
    if not _FAME_SOURCE.exists():
        return {}
    cutoff = as_of - timedelta(days=window_days)
    counts: dict[tuple[str, str], tuple[int, int]] = {}
    with _FAME_SOURCE.open("r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                row_date = date.fromisoformat(row["date"])
            except (KeyError, ValueError):
                continue
            if row_date >= as_of or row_date < cutoff:
                continue
            key = (_normalize(row["player_name"]), canonicalize_team(row["team"]))
            mp_inc = 1 if row.get("is_most_popular") == "1" else 0
            mp, total = counts.get(key, (0, 0))
            counts[key] = (mp + mp_inc, total + 1)
    return counts


def get_fame_rate(
    player_name: str,
    team: str,
    as_of: date,
    is_pitcher: bool,
) -> tuple[float, int]:
    """Public accessor: return (rate_in_0_to_1, denominator) for a player.

    `denominator` is exposed so the caller can distinguish "0 prior
    appearances" (rate=0, denom=0) from "appeared but never popular"
    (rate=0, denom>0) — the runtime score function should award fame
    points only when there's a meaningful denominator.
    """
    window = (
        LEVERAGE_FAME_INDEX_DAYS_PITCHER if is_pitcher
        else LEVERAGE_FAME_INDEX_DAYS_BATTER
    )
    lookup = _load_fame_rate_index(as_of, window)
    mp, total = lookup.get(
        (_normalize(player_name), canonicalize_team(team)),
        (0, 0),
    )
    if total == 0:
        return (0.0, 0)
    return (mp / total, total)


def _elite_stat_pts(
    is_pitcher: bool,
    season_ops: float | None,
    season_era: float | None,
) -> float:
    """Continuous elite-stat score in [0, LEVERAGE_ELITE_STAT_MAX_PTS].

    Replaces V15's binary thresholds (OPS >= 0.900 → +2 / ERA <= 3.00 → +2).
    Pitchers: lower ERA = higher pts (descending ramp).  Batters: higher
    OPS = higher pts (ascending ramp).  Linear within [floor, ceiling],
    clamped to [0, MAX_PTS] outside.

    Inputs are non-Optional from the caller's strict precondition, but
    handled defensively here (return 0.0 on missing) so this helper can
    be unit-tested in isolation without crashing on missing fixture data.
    """
    if is_pitcher:
        if season_era is None or season_era <= 0.0:
            # ERA = 0 occurs in opening-week 0-IP small-sample rows where
            # the byDateRange API returns garbage zeros.  Treat as
            # "no signal" (0 pts) rather than "ace tier" (max pts) — the
            # right behavior is conservative pending real innings.
            return 0.0
        floor = LEVERAGE_ELITE_PITCHER_ERA_FLOOR
        ceiling = LEVERAGE_ELITE_PITCHER_ERA_CEILING
        if season_era <= floor:
            return LEVERAGE_ELITE_STAT_MAX_PTS
        if season_era >= ceiling:
            return 0.0
        # Linear ramp: lower ERA → higher pts
        frac = (ceiling - season_era) / (ceiling - floor)
        return LEVERAGE_ELITE_STAT_MAX_PTS * frac

    if season_ops is None:
        return 0.0
    floor = LEVERAGE_ELITE_BATTER_OPS_FLOOR
    ceiling = LEVERAGE_ELITE_BATTER_OPS_CEILING
    if season_ops <= floor:
        return 0.0
    if season_ops >= ceiling:
        return LEVERAGE_ELITE_STAT_MAX_PTS
    # Linear ramp: higher OPS → higher pts
    frac = (season_ops - floor) / (ceiling - floor)
    return LEVERAGE_ELITE_STAT_MAX_PTS * frac


def popularity_score_to_multiplier(score: float | None) -> float:
    """Map a continuous popularity score in [0, ~10] to an EV multiplier.

    The curve is linear with clamps:
        multiplier = clamp(1.0 + (NEUTRAL - score) * SLOPE, FLOOR, CEILING)

    Higher score (more popular) → multiplier < 1.0 (consensus discount).
    Lower score (more contrarian) → multiplier > 1.0 (sleeper premium).
    A None score returns 1.0 — only place a default is acceptable, because
    the leverage signal is genuinely additive and a missing prediction
    must not corrupt a valid performance projection the way a missing
    ERA would.
    """
    if score is None:
        return 1.0
    raw = 1.0 + (POPULARITY_NEUTRAL_SCORE - score) * POPULARITY_SLOPE
    return max(POPULARITY_MULT_FLOOR, min(POPULARITY_MULT_CEILING, raw))


def _team_market_score(team: str, is_pitcher: bool, player_name: str) -> float:
    """Resolve team-market tier to a score component.

    Raises if the team is not in TEAM_MARKET_TIER — every team in
    PARK_HR_FACTORS must have a tier (enforced at startup by
    _validate_constants), so a runtime miss means a vendor-abbreviation
    drift the canonicaliser missed and is a real data-collection bug,
    not a missing-data event.  No silent fallback to neutral.
    """
    canonical = canonicalize_team(team)
    if canonical not in TEAM_MARKET_TIER:
        raise RuntimeError(
            f"predict_popularity_score: team {team!r} (canonical {canonical!r}) "
            f"not in TEAM_MARKET_TIER — for player {player_name!r} "
            f"(is_pitcher={is_pitcher}).  Add the team to TEAM_MARKET_TIER in "
            "app/core/constants.py or fix the upstream abbreviation."
        )
    tier = TEAM_MARKET_TIER[canonical]
    return {1: 2.0, 2: 2.0, 3: 1.0, 4: 0.0}[tier]


def _fame_rate_pts(
    player_name: str,
    team: str,
    as_of: date,
    is_pitcher: bool,
) -> float:
    """Continuous fame-rate score in [0, LEVERAGE_FAME_RATE_MAX_PTS].

    Replaces V15's binary fame_count thresholds (>= 1 → +1, >= 3 → +2).
    Returns 0 when the player has no prior appearances in the trailing
    window — they're either new to the corpus or have been off the
    leaderboards for >2 weeks (batter) / >4 weeks (pitcher), and the
    field has correspondingly low awareness of them.
    """
    rate, denom = get_fame_rate(player_name, team, as_of, is_pitcher)
    if denom == 0:
        return 0.0
    return LEVERAGE_FAME_RATE_MAX_PTS * rate


def predict_popularity_score(
    *,
    player_name: str,
    team: str,
    is_pitcher: bool,
    batting_order: int | None,
    season_ops: float | None,
    season_era: float | None,
    as_of: date,
) -> float:
    """Predict the field's popularity score for a TRADITIONAL-TRACK (non-rookie) player.

    Returns a float in roughly [0, 10] — higher = more popular = field
    will draft heavily.  The caller (`_compute_base_ev` via
    `popularity_score_to_multiplier`) maps it to an EV multiplier.

    Strict precondition (no silent fallbacks):
      * `team` MUST be in TEAM_MARKET_TIER.  Raises RuntimeError otherwise.
      * For batters, `season_ops` MUST be populated (the resolver runs
        `is_player_scoreable` which guarantees PA > 0 + Statcast power
        signal — OPS=None on a non-rookie batter is a data-collection bug).
      * For pitchers, `season_era` MUST be populated (same DNP filter
        guarantees IP > 0 + ERA on a non-rookie SP).

    Rookies have their own path — call `predict_rookie_popularity_score`
    instead.  Routing is done in the resolver based on
    PlayerStats.is_rookie_track.

    V15.1 scoring (continuous components, max ~10 points):
      Team market tier 1 = +3, 2 = +2, 3 = +1, 4 = 0
      STAR_PLAYER_FLAGS member = +3
        ELSE elite-stats ramp = [0, +2.5] continuous (OPS or ERA)
      Fame-rate ramp = [0, +3.0] continuous (mp_rate over trailing window)
      Top-3 batting order = +1 (binary; not fit, no historical data)
    """
    if is_pitcher and season_era is None:
        raise RuntimeError(
            f"predict_popularity_score: season_era=None for non-rookie pitcher "
            f"{player_name!r} ({team}) — every veteran SP must have ERA from "
            "fetch_player_season_stats / prior-season fallback.  If this is a "
            "rookie, route via predict_rookie_popularity_score instead."
        )
    if not is_pitcher and season_ops is None:
        raise RuntimeError(
            f"predict_popularity_score: season_ops=None for non-rookie batter "
            f"{player_name!r} ({team}) — every veteran batter past the DNP "
            "filter must have OPS.  If this is a rookie, route via "
            "predict_rookie_popularity_score instead."
        )

    score = _team_market_score(team, is_pitcher, player_name)

    name_norm = _normalize(player_name)
    if name_norm in STAR_PLAYER_FLAGS:
        score += 3.0
    else:
        score += _elite_stat_pts(is_pitcher, season_ops, season_era)

    score += _fame_rate_pts(player_name, team, as_of, is_pitcher)

    if not is_pitcher and batting_order is not None and 1 <= batting_order <= 3:
        score += 1.0

    return score


def predict_rookie_popularity_score(
    *,
    player_name: str,
    team: str,
    is_pitcher: bool,
    batting_order: int | None,
    as_of: date,
) -> float:
    """Predict popularity score for a TRUE MLB-DEBUTANT (rookie-track) player.

    The traditional path raises on missing OPS / ERA because for a veteran
    those gaps mean a data-collection bug.  Rookies have NO traditional
    stats by definition, so applying the strict precondition would crash
    every September call-up.

    Empirically the crowd fades rookies hard, so absent any contrary
    signal a rookie scores near 0 → multiplier near the ceiling
    (POPULARITY_MULT_CEILING).  The two ways a rookie can climb out:

      * Tier-1 market — Yankees / Dodgers / etc. fans draft their own
        team's call-ups regardless of MLB-debut status.
      * STAR_PLAYER_FLAGS hit — the elite prospect list (Holliday,
        Chourio, Langford, Merrill, etc.) should be pre-flagged in
        constants.py because they were household names before debuting.

    The fame_rate term is consulted but contributes near-zero for true
    rookies (no prior MP appearances by definition).  Batting order is
    still scored because a rookie batting leadoff WILL be drafted by
    his own market.

    Note: the V13.3 env cap (ROOKIE_ENV_MODIFIER_CEILING = 1.10) still
    applies to all rookies, so the popularity boost from a low score
    keeps rookies competitive in strong env without letting them
    dominate.  Under V15.7 (pitcher ceiling 1.30, popularity range
    [0.75, 1.55]) a rookie pitcher in a good matchup can earn
    ~1.10 × 1.0 × 1.55 = 1.705 EV multiplier, vs ~1.30 × 1.10 × 0.75
    = 1.073 for a comparable popular veteran ace — the rookie can
    actually beat a max-fame veteran on env+leverage in the same
    matchup, which is the V15.7 intent (pitcher TV is structurally
    capped, leverage is the dominant lever).  Veterans with strong
    trait_factor (1.20) and modest popularity (score 4-5, mult ~0.95)
    still out-EV rookies via trait, since rookie trait_factor is
    fixed at 1.0.
    """
    score = _team_market_score(team, is_pitcher, player_name)

    name_norm = _normalize(player_name)
    if name_norm in STAR_PLAYER_FLAGS:
        score += 3.0
    # No elite-stats branch — rookies have no current-season stats to
    # evaluate.  This is the deliberate carve-out, not a silent fallback.

    score += _fame_rate_pts(player_name, team, as_of, is_pitcher)

    if not is_pitcher and batting_order is not None and 1 <= batting_order <= 3:
        score += 1.0

    return score


def clear_cache() -> None:
    """Clear the cached fame-index lookups.  Tests use this to ensure
    each scenario starts from a clean read."""
    _load_fame_rate_index.cache_clear()
