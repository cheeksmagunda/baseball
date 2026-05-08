"""Lineup-TV outcome audit — V16 Phase 1's primary calibration metric.

For each historical slate, builds OUR lineup using the live runtime
composition logic (V15.3 1P-cap, per-team / per-game caps, anti-correlation
guard) on top of the same EV ranking the live runtime produces.  Then
sums the slot-weighted total_value outcome — the actual draft-win currency.

Compares:
  - Our lineup TV (mean / median / p25 / p75)
  - Rank-1 winning lineup TV (joined from historical_winning_drafts.csv
    via historical_players.csv for boost recovery)
  - Top-N winning ranks for context

This is the metric that decides V16 calibration — slot-1 hit-rate and
HV-rate@K are proxies; lineup TV is the actual win condition.

Same isolation rule as audit_hv_hit_rate.py: outcome columns
(real_score, total_value) are read but only as response variables;
the runtime never reads them.

Usage:
    BO_CURRENT_SEASON=2026 .venv/bin/python scripts/audit_lineup_tv.py

Sweep parameters via env vars (same as audit_hv_hit_rate.py):
    BO_OVERRIDE_TRAIT_MODIFIER_FLOOR / CEILING
    BO_OVERRIDE_ENV_MODIFIER_FLOOR / CEILING
    BO_OVERRIDE_PITCHER_ENV_MODIFIER_CEILING
    BO_OVERRIDE_POPULARITY_*
    BO_OVERRIDE_STACK_BONUS
    BO_OVERRIDE_MAX_PLAYERS_PER_TEAM_BATTERS_STACKABLE
    BO_OVERRIDE_MAX_PLAYERS_PER_GAME_BATTERS
    BO_DROP_POSITION_VOLUME=1                # bypass POSITION_VOLUME_MULTIPLIER
    V16_REAL_TRAIT=1                         # use Statcast-driven trait_factor
                                             # (default: flat 1.0 for V15.7 parity)
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date as DateType
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")

from app.core import constants as _C  # noqa: E402
from app.core.constants import (  # noqa: E402
    SLOT_MULTIPLIERS,
    canonicalize_team,
)
from app.services.filter_strategy import (  # noqa: E402
    _build_variant,
    _lineup_total_ev,
)


def _get(name: str) -> float:
    return getattr(_C, name)


# Reuse the override helper + scoring from audit_hv_hit_rate to keep a
# single source of truth.  Apply BO_OVERRIDE_ env vars BEFORE the
# scorer's first call so it picks up the patched constants.
import scripts.audit_hv_hit_rate as h  # noqa: E402
from scripts.audit_hv_hit_rate import (  # noqa: E402
    _maybe_override,
    load_slate_envs,
    neutral_total_score,
    score_one_player,
    slate_stack_eligible_teams,
    is_pitcher_pos,
)


def _setup_overrides() -> None:
    for v in [
        "ENV_MODIFIER_FLOOR",
        "ENV_MODIFIER_CEILING",
        "PITCHER_ENV_MODIFIER_CEILING",
        "ROOKIE_ENV_MODIFIER_CEILING",
        "POPULARITY_NEUTRAL_SCORE",
        "POPULARITY_SLOPE",
        "POPULARITY_MULT_FLOOR",
        "POPULARITY_MULT_CEILING",
        "STACK_BONUS",
        "TRAIT_MODIFIER_FLOOR",
        "TRAIT_MODIFIER_CEILING",
        "MAX_PLAYERS_PER_TEAM_BATTERS_STACKABLE",
        "MAX_PLAYERS_PER_GAME_BATTERS",
    ]:
        _maybe_override(v)


_setup_overrides()

if os.environ.get("V16_REAL_TRAIT") != "1":
    # Default: flat trait to match V15.7 baseline.
    h.compute_trait_score_from_csv = lambda *a, **kw: None


def build_team_to_game_pk(slate_results: list) -> dict[str, dict[str, int]]:
    """{date: {team: game_pk}} — to set candidate.game_id correctly.

    A game_pk uniquely identifies a game in the MLB schedule; both teams
    in a game share the same game_pk.  We use it as the game_id for the
    composition's per-game cap and anti-correlation guard.
    """
    out: dict[str, dict[str, int]] = {}
    for slate in slate_results:
        date = slate["date"]
        m: dict[str, int] = {}
        for g in slate.get("games", []):
            pk = g.get("game_pk")
            if pk is None:
                continue
            home = canonicalize_team(g["home"])
            away = canonicalize_team(g["away"])
            m[home] = pk
            m[away] = pk
        out[date] = m
    return out


def build_player_outcome_index(historical_players: Path) -> dict:
    """{(date, name, team): (rs, tv)}"""
    idx: dict = {}
    with historical_players.open() as f:
        for row in csv.DictReader(f):
            try:
                rs = float(row.get("real_score") or "")
                tv = float(row.get("total_value") or "")
            except ValueError:
                continue
            idx[(row["date"], row["player_name"], canonicalize_team(row["team"]))] = (rs, tv)
    return idx


def compute_winning_lineup_tvs(winning_drafts: Path, tv_idx: dict) -> dict:
    by_slate: dict = defaultdict(lambda: defaultdict(float))
    matched: dict = defaultdict(lambda: defaultdict(int))
    with winning_drafts.open() as f:
        for row in csv.DictReader(f):
            date = row["date"]
            try:
                rank = int(row["winner_rank"])
                slot_mult = float(row["slot_mult"])
                rs = float(row["real_score"])
            except ValueError:
                continue
            team = canonicalize_team(row["team"])
            key = (date, row["player_name"], team)
            if key not in tv_idx:
                # Player not on a leaderboard that day; can't recover boost.
                # Conservative: count their slot RS-only contribution (boost=0)
                # so the winning-lineup TV isn't artificially zeroed.
                by_slate[date][rank] += rs * slot_mult
                matched[date][rank] += 1
                continue
            row_rs, row_tv = tv_idx[key]
            boost = (row_tv / row_rs - 2.0) if row_rs > 0 else 0.0
            by_slate[date][rank] += rs * (slot_mult + boost)
            matched[date][rank] += 1
    cleaned: dict = {}
    for date, ranks in by_slate.items():
        cleaned[date] = {r: tv for r, tv in ranks.items() if matched[date][r] == 5}
    return cleaned


class _Cand(SimpleNamespace):
    """Duck-typed minimal FilteredCandidate — exposes attrs the composition
    layer reads (`team`, `game_id`, `is_pitcher`, `filter_ev`).
    """


def make_slate_class(eligible_teams: set[str], slate_games: list) -> SimpleNamespace:
    """Build a minimal SlateClassification for _compute_stack_eligible_teams.

    The harness already computes eligible teams via slate_stack_eligible_teams;
    we wrap them in stackable-games stubs so the runtime function returns
    the same set.
    """
    stackable = []
    seen: set[int] = set()
    for g in slate_games:
        pk = g.get("game_pk")
        if pk in seen or pk is None:
            continue
        seen.add(pk)
        home = canonicalize_team(g["home"])
        away = canonicalize_team(g["away"])
        # Build one StackableGame-like for each side that's eligible.
        for side, team, opp in [("home", home, away), ("away", away, home)]:
            if team not in eligible_teams:
                continue
            ml = g.get(f"{side}_moneyline")
            opp_era = g.get(
                f"{opp.lower() if False else 'home' if side == 'away' else 'away'}_starter_era"
            )
            # Simpler: read opp_era from the opposite-side fields
            opp_era = g.get("away_starter_era") if side == "home" else g.get("home_starter_era")
            own_ops = g.get(f"{side}_team_ops")
            stackable.append(
                SimpleNamespace(
                    favored_team=team,
                    moneyline=ml,
                    vegas_total=g.get("vegas_total"),
                    opp_starter_era=opp_era,
                    own_team_ops=own_ops,
                )
            )
    return SimpleNamespace(stackable_games=stackable)


def lineup_tv_outcome(lineup: list, tv_idx: dict, slate_date: str) -> tuple[float, int]:
    """Slot-weighted TV: assign slot-mults by EV desc and sum rs × (slot_mult + boost)."""
    if len(lineup) != 5:
        return 0.0, 0
    sorted_lineup = sorted(lineup, key=lambda c: c.filter_ev, reverse=True)
    mults = sorted(SLOT_MULTIPLIERS.values(), reverse=True)
    total = 0.0
    matched = 0
    for cand, mult in zip(sorted_lineup, mults):
        team = cand.team.upper()
        key = (slate_date, cand.player_name, team)
        if key not in tv_idx:
            continue
        rs, tv = tv_idx[key]
        boost = (tv / rs - 2.0) if rs > 0 else 0.0
        total += rs * (mult + boost)
        matched += 1
    return total, matched


def main() -> int:
    historical_csv = ROOT / "data" / "historical_players.csv"
    winning_csv = ROOT / "data" / "historical_winning_drafts.csv"
    slate_results_json = ROOT / "data" / "historical_slate_results.json"

    with slate_results_json.open() as f:
        slate_results_data = json.load(f)
    team_to_pk = build_team_to_game_pk(slate_results_data)
    slate_envs = load_slate_envs(slate_results_json)
    tv_idx = build_player_outcome_index(historical_csv)
    win_lineups = compute_winning_lineup_tvs(winning_csv, tv_idx)

    rows_by_date: dict[str, list[dict]] = defaultdict(list)
    with historical_csv.open() as f:
        for row in csv.DictReader(f):
            rows_by_date[row["date"]].append(row)

    NEUTRAL = neutral_total_score()
    our_tvs: list[float] = []
    rank1_tvs: list[float] = []
    rank3_avg_tvs: list[float] = []
    rank20_avg_tvs: list[float] = []
    full_match: int = 0

    skipped_no_lineup: int = 0

    for date_str in sorted(rows_by_date):
        if date_str not in slate_envs:
            continue
        env_lookup = slate_envs[date_str]
        eligible = slate_stack_eligible_teams(env_lookup)
        as_of = DateType.fromisoformat(date_str)
        team_pk = team_to_pk.get(date_str, {})

        # Score every player and build duck-typed candidates with game_id.
        candidates: list[_Cand] = []
        for row in rows_by_date[date_str]:
            rec = score_one_player(row, env_lookup, eligible, as_of, NEUTRAL)
            if rec is None:
                continue
            team = canonicalize_team(row["team"])
            game_pk = team_pk.get(team)
            candidates.append(
                _Cand(
                    player_name=row["player_name"],
                    team=team,
                    position=row.get("position", ""),
                    is_pitcher=is_pitcher_pos(row.get("position", "")),
                    game_id=game_pk,
                    filter_ev=rec["filter_ev"],
                )
            )

        if len(candidates) < 5:
            continue

        # Build lineup using runtime composition (V15.3 1P-cap).
        sorted_pitchers = sorted(
            [c for c in candidates if c.is_pitcher], key=lambda c: c.filter_ev, reverse=True
        )
        sorted_batters = sorted(
            [c for c in candidates if not c.is_pitcher], key=lambda c: c.filter_ev, reverse=True
        )

        best_lineup: list = []
        best_ev: float = -1.0
        for n_p in range(0, _get("MAX_PITCHERS_PER_LINEUP") + 1):
            lineup = _build_variant(n_p, sorted_pitchers, sorted_batters, eligible)
            if len(lineup) != 5:
                continue
            ev = _lineup_total_ev(lineup)
            if ev > best_ev:
                best_ev = ev
                best_lineup = lineup

        if not best_lineup:
            skipped_no_lineup += 1
            continue

        our_tv, matched = lineup_tv_outcome(best_lineup, tv_idx, date_str)
        our_tvs.append(our_tv)
        if matched == 5:
            full_match += 1

        ranks = win_lineups.get(date_str, {})
        if 1 in ranks:
            rank1_tvs.append(ranks[1])
        rs = sorted(ranks.values(), reverse=True)
        if rs:
            rank3_avg_tvs.append(sum(rs[:3]) / min(3, len(rs)))
            rank20_avg_tvs.append(sum(rs[:20]) / min(20, len(rs)))

    def stats(label: str, vals: list[float]) -> None:
        if not vals:
            print(f"  {label}: empty")
            return
        s = sorted(vals)
        n = len(vals)
        mean = sum(vals) / n
        median = s[n // 2]
        p25 = s[n // 4]
        p75 = s[3 * n // 4]
        print(
            f"  {label}: n={n}  mean={mean:.1f}  median={median:.1f}  p25={p25:.1f}  p75={p75:.1f}  min={min(vals):.1f}  max={max(vals):.1f}"
        )

    print("=== V16 Phase 1: Lineup TV outcome audit ===")
    print()
    if os.environ.get("V16_REAL_TRAIT") == "1":
        print("  trait_factor: V16 real Statcast-driven")
    else:
        print("  trait_factor: FLAT 1.0 (V15.7 baseline parity)")
    pos_vol_dict = getattr(_C, "POSITION_VOLUME_MULTIPLIER", {})
    if os.environ.get("BO_DROP_POSITION_VOLUME") == "1":
        print("  POSITION_VOLUME_MULTIPLIER: DISABLED via env override")
    elif not pos_vol_dict:
        print(
            "  POSITION_VOLUME_MULTIPLIER: removed (V16 Phase 1) — empty dict, all positions = 1.0"
        )
    else:
        print(f"  POSITION_VOLUME_MULTIPLIER: active {dict(pos_vol_dict)}")
    print(f"  TRAIT band [{_get('TRAIT_MODIFIER_FLOOR')}, {_get('TRAIT_MODIFIER_CEILING')}]")
    print(
        f"  Stack cap: {_get('MAX_PLAYERS_PER_TEAM_BATTERS_STACKABLE')} batters/team in stack-eligible games"
    )
    print(f"  Per-game cap: {_get('MAX_PLAYERS_PER_GAME_BATTERS')} batters/game")
    print(f"  STACK_BONUS: {_get('STACK_BONUS')}")
    print()
    stats("Our lineup TV", our_tvs)
    print(f"  Slates with all 5 picks scoring: {full_match}/{len(our_tvs)}")
    print(f"  Slates skipped (no legal lineup): {skipped_no_lineup}")
    print()
    stats("Rank-1 winning lineup TV", rank1_tvs)
    stats("Mean of top-3 winning lineups TV", rank3_avg_tvs)
    stats("Mean of top-20 winning lineups TV", rank20_avg_tvs)
    print()
    if our_tvs and rank1_tvs:
        gap1 = sum(rank1_tvs) / len(rank1_tvs) - sum(our_tvs) / len(our_tvs)
        gap20 = sum(rank20_avg_tvs) / len(rank20_avg_tvs) - sum(our_tvs) / len(our_tvs)
        print(f"  Gap to rank-1 mean:  {gap1:+.1f} TV/slate")
        print(f"  Gap to top-20 mean:  {gap20:+.1f} TV/slate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
