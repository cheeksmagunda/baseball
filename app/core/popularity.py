"""Predicted-ownership bucket for each candidate (V14, May 2026).

The pipeline is, by construction, a performance predictor.  The 40-slate
audit (STRATEGY_AUDIT_2026-05.md) shows that performance prediction alone
cannot win Real Sports daily drafts: 92.5% of historical winning lineups
contain at least one Highest Value player who was not on the Most Popular
leaderboard, and popular HVs vs sleeper HVs score essentially identically.
The contest-winning edge is differentiation from the field, not raw
projection.

This module produces, for each candidate, a discrete bucket label
predicting where the field will draft them tomorrow.  The bucket feeds
`leverage_factor` in `_compute_base_ev`, multiplying EV by ∈ [0.85, 1.20]
so that two candidates with identical env+trait scores rank by ownership
disparity rather than alphabetical accident.

Inputs (all pre-game public observables):
    1. Team market tier — TEAM_MARKET_TIER lookup in constants.py.
       Yankees / Dodgers / Cubs etc. are systematically over-drafted; small
       markets are systematically under-drafted.  Static; not an outcome.
    2. Player fame — STAR_PLAYER_FLAGS (returning All-Stars, MVP/CY top-5)
       plus elite current-season stats (OPS >= 0.900 / ERA <= 3.00).
       Both are facts visible on every player profile pre-game.
    3. Slate context — top-of-order batting position.
    4. Rolling 14-day fame index — count of prior Most Popular leaderboard
       appearances in the trailing window.  This is the one feature that
       references historical data, and only the prior-slate Most Popular
       flag (a publicly-displayed observable, not an outcome label of the
       current slate).  The audit doc explicitly carves this out as
       analogous to using prior-season ERA: a backward-looking aggregate
       of pre-game observables, not leakage of the current slate's label.

Inputs that are FORBIDDEN by the architecture and not consumed here:
    - `card_boost` (revealed only during/after draft)
    - `drafts` (raw historical count is an outcome label)
    - `real_score`, `total_value`, `is_highest_value` (post-game truth)

The bucket function is deterministic and rule-based.  No statistical
model, no learned weights — bucket boundaries are quantile-derived from
the 40-slate corpus and tuned manually via the standard calibration
discipline that governs every other constant in app/core/constants.py.
"""

from __future__ import annotations

import csv
import unicodedata
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from app.core.constants import (
    LEVERAGE_FAME_INDEX_DAYS,
    LEVERAGE_STAR_BATTER_OPS,
    LEVERAGE_STAR_PITCHER_ERA,
    STAR_PLAYER_FLAGS,
    TEAM_MARKET_TIER,
    canonicalize_team,
)


# Path to the prior-slate fame source.  Same file as the calibration
# corpus, but only its date + player_name + team + is_most_popular columns
# are consumed here, and only for dates strictly before the current slate.
# The audit script (scripts/audit_live_isolation.py) exempts this module
# precisely because the read is bounded and the field is a pre-game
# observable for any future slate.
_FAME_SOURCE = Path(__file__).resolve().parents[2] / "data" / "historical_players.csv"


def _normalize(name: str) -> str:
    """Same normalization as app.models.player.normalize_name.

    Local copy so this module has no SQLAlchemy import dependency — it can
    be called from non-DB contexts (offline calibration, tests).
    """
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(ascii_name.lower().split())


@lru_cache(maxsize=4)
def _load_fame_index(as_of: date) -> dict[tuple[str, str], int]:
    """Build {(name_normalized, team): MP_appearances_in_prior_14_days}.

    Cached per `as_of` date — a single T-65 pipeline run scores ~250
    candidates and would otherwise re-read the CSV that many times.

    Only rows strictly older than `as_of` and within
    LEVERAGE_FAME_INDEX_DAYS are counted.  The current-slate row (if it
    were present in the CSV ahead of time, which it is not) would be
    excluded — the function does not see today's outcome.
    """
    if not _FAME_SOURCE.exists():
        return {}
    cutoff = as_of - timedelta(days=LEVERAGE_FAME_INDEX_DAYS)
    counts: dict[tuple[str, str], int] = {}
    with _FAME_SOURCE.open("r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                row_date = date.fromisoformat(row["date"])
            except (KeyError, ValueError):
                continue
            if row_date >= as_of or row_date < cutoff:
                continue
            if row.get("is_most_popular") != "1":
                continue
            key = (_normalize(row["player_name"]), canonicalize_team(row["team"]))
            counts[key] = counts.get(key, 0) + 1
    return counts


def get_fame_index(player_name: str, team: str, as_of: date) -> int:
    """Public accessor for the rolling 14-day MP appearance count."""
    lookup = _load_fame_index(as_of)
    return lookup.get((_normalize(player_name), canonicalize_team(team)), 0)


def _is_star_by_stats(is_pitcher: bool, season_ops: float, season_era: float) -> bool:
    """True if current-season aggregates put the player in name-recognition territory.

    Catches breakouts who are not in STAR_PLAYER_FLAGS yet — e.g. an
    OPS-0.950 hitter who broke out mid-season will be drafted heavily
    even without prior fame.

    Inputs are non-Optional: the caller is responsible for confirming the
    player is on the traditional (non-rookie) track and PlayerStats is
    populated before invoking this branch.  See predict_popularity_bucket
    for the strict precondition.
    """
    if is_pitcher:
        return season_era <= LEVERAGE_STAR_PITCHER_ERA
    return season_ops >= LEVERAGE_STAR_BATTER_OPS


def _bucket_from_score(score: float) -> str:
    """Map an internal popularity score in roughly [0, 10] to a discrete bucket.

    Quantile-derived cutoffs.  Re-tune via the standard calibration
    discipline when the corpus expands.
    """
    if score >= 8.0:
        return "top_decile"
    if score >= 6.0:
        return "upper_mid"
    if score >= 3.0:
        return "mid"
    if score >= 1.5:
        return "lower_mid"
    return "bottom_decile"


def _team_market_score(team: str, is_pitcher: bool, player_name: str) -> float:
    """Resolve team-market tier to a score component.

    Raises if the team is not in TEAM_MARKET_TIER — every team in
    PARK_HR_FACTORS must have a tier (enforced at startup by
    _validate_constants), so a runtime miss means a vendor-abbreviation
    drift the canonicaliser missed and is a real data-collection bug,
    not a missing-data event.  No silent fallback to 'mid'.
    """
    canonical = canonicalize_team(team)
    if canonical not in TEAM_MARKET_TIER:
        raise RuntimeError(
            f"predict_popularity_bucket: team {team!r} (canonical {canonical!r}) "
            f"not in TEAM_MARKET_TIER — for player {player_name!r} "
            f"(is_pitcher={is_pitcher}).  Add the team to TEAM_MARKET_TIER in "
            "app/core/constants.py or fix the upstream abbreviation."
        )
    tier = TEAM_MARKET_TIER[canonical]
    return {1: 3.0, 2: 2.0, 3: 1.0, 4: 0.0}[tier]


def predict_popularity_bucket(
    *,
    player_name: str,
    team: str,
    is_pitcher: bool,
    batting_order: int | None,
    season_ops: float | None,
    season_era: float | None,
    as_of: date,
) -> str:
    """Predict the field's draft bucket for a TRADITIONAL-TRACK (non-rookie) player.

    Returns one of: top_decile, upper_mid, mid, lower_mid, bottom_decile.

    Strict precondition (no silent fallbacks):
      * `team` MUST be in TEAM_MARKET_TIER.  Raises RuntimeError otherwise.
      * For batters, `season_ops` MUST be populated (the resolver runs
        `is_player_scoreable` which guarantees PA > 0 + Statcast power
        signal — OPS=None on a non-rookie batter is a data-collection bug).
      * For pitchers, `season_era` MUST be populated (same DNP filter
        guarantees IP > 0 + ERA on a non-rookie SP).

    Rookies have their own path — call `predict_rookie_popularity_bucket`
    instead.  Routing is done in the resolver based on
    PlayerStats.is_rookie_track.

    Scoring (max ~10 points):
      Team market tier 1 = +3, 2 = +2, 3 = +1, 4 = 0
      STAR_PLAYER_FLAGS member = +3
      Elite current-season stats = +2 (only if NOT already a flagged star,
        to avoid double-counting)
      Rolling fame index >= 3 = +2, >= 1 = +1
      Top-3 batting order = +1 (top-of-order PA volume drives draft)
    """
    if is_pitcher and season_era is None:
        raise RuntimeError(
            f"predict_popularity_bucket: season_era=None for non-rookie pitcher "
            f"{player_name!r} ({team}) — every veteran SP must have ERA from "
            "fetch_player_season_stats / prior-season fallback.  If this is a "
            "rookie, route via predict_rookie_popularity_bucket instead."
        )
    if not is_pitcher and season_ops is None:
        raise RuntimeError(
            f"predict_popularity_bucket: season_ops=None for non-rookie batter "
            f"{player_name!r} ({team}) — every veteran batter past the DNP "
            "filter must have OPS.  If this is a rookie, route via "
            "predict_rookie_popularity_bucket instead."
        )

    score = _team_market_score(team, is_pitcher, player_name)

    name_norm = _normalize(player_name)
    if name_norm in STAR_PLAYER_FLAGS:
        score += 3.0
    elif _is_star_by_stats(is_pitcher, season_ops or 0.0, season_era or 9.99):
        score += 2.0

    fame = get_fame_index(player_name, team, as_of)
    if fame >= 3:
        score += 2.0
    elif fame >= 1:
        score += 1.0

    if not is_pitcher and batting_order is not None and 1 <= batting_order <= 3:
        score += 1.0

    return _bucket_from_score(score)


def predict_rookie_popularity_bucket(
    *,
    player_name: str,
    team: str,
    is_pitcher: bool,
    batting_order: int | None,
    as_of: date,
) -> str:
    """Predict ownership bucket for a TRUE MLB-DEBUTANT (rookie-track) player.

    The traditional path raises on missing OPS / ERA because for a veteran
    those gaps mean a data-collection bug.  Rookies have NO traditional
    stats by definition (zero current-season + zero prior-season + below
    the rookie experience threshold), so applying the strict precondition
    would crash every September call-up.

    Empirically the crowd fades rookies hard (median draft count ~ tens
    rather than hundreds), so absent any contrary signal a rookie defaults
    deep into bottom_decile.  The two ways a rookie can climb out:

      * Tier-1 market — Yankees / Dodgers / etc. fans draft their own
        team's call-ups regardless of MLB-debut status.
      * STAR_PLAYER_FLAGS hit — the elite prospect list (Holliday,
        Chourio, Langford, Merrill, etc.) should be pre-flagged in
        constants.py because they were household names before debuting.

    The fame_index is consulted but contributes near-zero for true
    rookies (no prior MP appearances by definition).  Batting order is
    still scored because a rookie batting leadoff WILL be drafted by
    his own market.

    Inputs:
      Same as predict_popularity_bucket EXCEPT season_ops/season_era are
      not consumed (rookies have neither).  team and as_of are still
      validated.  No silent default for unknown team.
    """
    score = _team_market_score(team, is_pitcher, player_name)

    name_norm = _normalize(player_name)
    if name_norm in STAR_PLAYER_FLAGS:
        score += 3.0
    # No is_star_by_stats branch — rookies have no current-season stats
    # to evaluate.  This is the deliberate carve-out, not a silent fallback.

    fame = get_fame_index(player_name, team, as_of)
    if fame >= 3:
        score += 2.0
    elif fame >= 1:
        score += 1.0

    if not is_pitcher and batting_order is not None and 1 <= batting_order <= 3:
        score += 1.0

    return _bucket_from_score(score)


def clear_cache() -> None:
    """Clear the cached fame-index lookups.  Tests use this to ensure
    each scenario starts from a clean read."""
    _load_fame_index.cache_clear()
