"""Fixed constants for the DFS scoring engine."""

# Draft slot multipliers (slot 1 = highest)
SLOT_MULTIPLIERS = {1: 2.0, 2: 1.8, 3: 1.6, 4: 1.4, 5: 1.2}

# ---------------------------------------------------------------------------
# Team abbreviation canonicalization
# Maps variant abbreviations to the canonical form used in PARK_HR_FACTORS.
# Strategy doc §1.3 Issue 2: KC/KCR and CWS/CHW inconsistencies.
# ---------------------------------------------------------------------------
TEAM_ABBR_ALIASES = {
    "KCR": "KC",
    "CHW": "CWS",
    "AZ": "ARI",
    "WSN": "WSH",
    "TBR": "TB",
    "SDP": "SD",
    "SFG": "SF",
    "OAK": "ATH",  # Athletics relocated to Sacramento; MLB API now returns ATH
}


def canonicalize_team(abbr: str) -> str:
    """Normalize a team abbreviation to the canonical form."""
    upper = abbr.strip().upper()
    return TEAM_ABBR_ALIASES.get(upper, upper)

# Ballpark HR factors (relative to league average = 1.0)
# Values > 1.0 favor hitters; < 1.0 favor pitchers
PARK_HR_FACTORS = {
    "COL": 1.38,  # Coors Field
    "CIN": 1.18,  # Great American
    "PHI": 1.12,  # Citizens Bank
    "HOU": 1.10,  # Minute Maid
    "TEX": 1.08,  # Globe Life
    "CHC": 1.06,  # Wrigley
    "BAL": 1.05,  # Camden Yards
    "TOR": 1.04,  # Rogers Centre
    "NYY": 1.03,  # Yankee Stadium
    "BOS": 1.02,  # Fenway
    "MIL": 1.02,  # American Family
    "MIN": 1.01,  # Target Field
    "ATL": 1.00,  # Truist Park
    "CLE": 1.00,  # Progressive
    "DET": 0.99,  # Comerica
    "ARI": 0.98,  # Chase Field
    "STL": 0.98,  # Busch
    "CWS": 0.97,  # Guaranteed Rate
    "WSH": 0.97,  # Nationals Park
    "KC": 0.96,   # Kauffman
    "PIT": 0.96,  # PNC Park
    "LAA": 0.95,  # Angel Stadium
    "NYM": 0.95,  # Citi Field
    "TB": 0.94,   # Tropicana
    "SD": 0.93,   # Petco Park
    "SF": 0.92,   # Oracle Park
    "SEA": 0.91,  # T-Mobile
    "MIA": 0.90,  # loanDepot
    "ATH": 1.09,  # Sacramento (Sutter Health Park) — 2026 Statcast PF = 1.091; short porch RF favors LHB
    "LAD": 0.89,  # Dodger Stadium
}

# Standard positions
PITCHER_POSITIONS = {"P", "SP", "RP"}
BATTER_POSITIONS = {"C", "1B", "2B", "3B", "SS", "OF", "DH"}
ALL_POSITIONS = PITCHER_POSITIONS | BATTER_POSITIONS

# Teams
MLB_TEAMS = sorted(PARK_HR_FACTORS.keys())

# Minimum score threshold for the trait_factor mapping.  Players below this
# get the floor trait_factor (0.85).
MIN_SCORE_THRESHOLD = 15  # out of 100

# ---------------------------------------------------------------------------
# Filter Strategy constants (§4 "Filter, Not Forecast")
# ---------------------------------------------------------------------------

# Slate classification thresholds (Filter 1)
# Historical distribution: Pitcher Day = 23% of slates, Hitter/Stack Day = 38%
TINY_SLATE_MAX_GAMES = 3
PITCHER_DAY_MIN_QUALITY_SP = 4   # 4+ quality SP matchups → pitcher day (§3)
HITTER_DAY_MIN_HIGH_TOTAL = 4    # 4+ games with O/U >= 9.0 → hitter day (§3)
HITTER_DAY_VEGAS_TOTAL_THRESHOLD = 9.0

# Blowout detection (§2 Pillar 2 + §3 checklist)
# Moneyline ≥ -200 for one side = projected blowout → stack candidate
BLOWOUT_MONEYLINE_THRESHOLD = -200  # e.g. -210 means heavy favorite
BLOWOUT_MIN_GAMES_FOR_STACK_DAY = 1  # 1+ blowout game → stack day eligible

# V10.2 conservative stacking architecture.
#
# Stacking is powerful (pitcher shuts down opposing offense → teammates cash
# the run/RBI bonuses) but it is also correlated — when the favorite loses,
# an oversized stack crashes the whole lineup.  The strategy doc only
# recommends stacks when the game script is overwhelmingly favorable, and
# even then a MINI-stack (two teammates) captures most of the correlation
# edge without the correlated-downside tail.
#
# A team is stack-eligible if its game satisfies ANY of:
#
#   PATH 1 (BLOWOUT FAVORITE — favored side only, earns STACK_BONUS):
#     moneyline ≤ STACK_ELIGIBILITY_MONEYLINE  AND
#     Vegas O/U ≥ STACK_ELIGIBILITY_VEGAS_TOTAL
#
#   PATH 2 (EXTREME SHOOTOUT — both sides eligible, no bonus):
#     Vegas O/U ≥ STACK_ELIGIBILITY_SHOOTOUT_TOTAL
#
#   PATH 3 (CATASTROPHIC OPPOSING STARTER — favored side only, no bonus):
#     opposing starter season ERA ≥ STACK_ELIGIBILITY_PATH3_OPP_SP_ERA  AND
#     own team season OPS ≥ STACK_ELIGIBILITY_PATH3_OWN_TEAM_OPS
#
# PATH 1 captures predictable blowouts (favorite scores; opposing pitcher
# shelled).  PATH 2 captures Coors-class shootouts where both lineups
# project to feast regardless of which side wins — those games are
# "glaringly obvious" run environments where mini-stacking either side
# is well-supported by Vegas.  PATH 3 catches the case Vegas missed: a
# capable lineup facing a starter whose ERA flags genuine blow-up risk
# (Strider just back from IL, a season-debut SP, a journeyman on a hot
# bad stretch).  A 6.5+ ERA SP on a starter who has survived 30+ IP is
# rare enough that the gate fires on roughly 1 in 3 slates and adds <0.5
# new stack-eligible teams per slate on average.  No STACK_BONUS — the
# bonus stays gated to PATH 1 where Vegas itself priced the favorite.
#
# All other teams fall back to the one-batter-per-team default.  A heavy
# favorite in a low-scoring pitcher's duel (-220 with O/U 7.0) is NOT
# stack-eligible — fails all three paths.
STACK_ELIGIBILITY_MONEYLINE = -200     # favorite threshold (PATH 1)
STACK_ELIGIBILITY_VEGAS_TOTAL = 9.0    # min O/U paired with ML in PATH 1
STACK_ELIGIBILITY_SHOOTOUT_TOTAL = 10.5  # min O/U for ML-agnostic shootout PATH 2
STACK_ELIGIBILITY_PATH3_OPP_SP_ERA = 6.5   # opp starter ERA floor for PATH 3
STACK_ELIGIBILITY_PATH3_OWN_TEAM_OPS = 0.760  # own team OPS floor for PATH 3 (above-avg offense)

# Caps applied downstream of the stack-eligibility gate.  MAX=2 is the
# deliberate mini-stack ceiling — never more than two teammates in a
# lineup, regardless of how overwhelming the game script is.
MAX_PLAYERS_PER_TEAM_BATTERS_STACKABLE = 2  # 2-batter mini-stack on eligible teams
MAX_PLAYERS_PER_TEAM_BATTERS_DEFAULT = 1    # every other team: one batter per lineup

# Independent per-game cap: even across opposing teams, never more than two
# batters from the same game.  Combined with the "no opposing batter in the
# anchor's game" rule this means: the anchor's game may contribute at most
# two batters (the anchor's teammates); every other game may contribute at
# most two batters (from the same team, since mixing sides in a non-anchor
# game is naturally allowed but capped here).
MAX_PLAYERS_PER_GAME_BATTERS = 2

# V15.3 (May 6, 2026): Real Sports platform caps lineups at 1 pitcher.
# V12 had relaxed this to 0..5 ("multi-pitcher 0P-5P composition chooser")
# based on a 35-slate audit showing winning lineups had multi-pitcher
# shapes (28.6% 2P+3B, 14.3% 3P+2B, 11.4% 4P+1B).  That audit data is in
# tension with the platform's actual rule and is now considered suspect
# — most likely the audit was contaminated, the platform changed since,
# or the audit's "winning lineup" capture mis-labelled positions.
# Either way the runtime behaviour must respect the platform constraint:
# a 4P+1B lineup is unsubmittable.  Variant chooser (`_enforce_composition`)
# now iterates only over n_p in [0, MAX_PITCHERS_PER_LINEUP].
MAX_PITCHERS_PER_LINEUP = 1

MIN_GAMES_REPRESENTED = 2        # pipeline-level data-sufficiency guard (not a lineup rule)


# ---------------------------------------------------------------------------
# Rookie scoring track
# ---------------------------------------------------------------------------
# True MLB debutants — players with no current-season stats AND no prior-season
# fallback hit — are the only legitimate "missing traditional stats" case.
# They go through a separate scorer in scoring_engine that uses Statcast
# kinematics + env signals only, with neutral trait_factor.  Every other case
# (returning veteran, traded mid-day, IL returnee with last-year MLB stats)
# is covered by the prior-season fallback in fetch_player_season_stats and
# scored on the normal track.
#
# A player qualifies for the rookie track when, AFTER the prior-season
# fallback runs, the relevant traditional stats are still missing AND the
# player has fewer than ROOKIE_GAMES_THRESHOLD MLB games of experience for
# batters / less than ROOKIE_PITCHER_IP_THRESHOLD career IP for pitchers.
# These thresholds also gate the strict-assertion bypass in
# pipeline.run_fetch_player_stats — a 5-year veteran missing ERA is still
# a hard crash (real bug), only true debutants skip the gate.
ROOKIE_GAMES_THRESHOLD = 3        # batters: < this many career MLB games
ROOKIE_PITCHER_IP_THRESHOLD = 5.0 # pitchers: < this much career MLB IP

# V13.3 (May 2026): tighten the rookie-pitcher gate.  A pitcher with
# current-season IP=0 AND prior-season IP < this threshold has too thin
# a sample for the recent-stats fallback to be reliable.  Trevor McDonald
# (SF, 2026-05-04) had 3 IP / 0.00 ERA / 0.33 WHIP from a single 2024
# start — the strict pipeline trusted those numbers at face value, which
# combined with V13's underdog-peak ML reward saturated his env_factor
# and pushed him to top-EV pitcher.  Forcing rookie-track for thin-prior
# pitchers makes the rookie env ceiling apply (1.10), removing the
# "underdog spot starter" trap.
PITCHER_FALLBACK_MIN_PRIOR_IP = 30.0


def is_stack_eligible_game(
    moneyline: int | None,
    vegas_total: float | None,
    opp_starter_era: float | None = None,
    own_team_ops: float | None = None,
) -> bool:
    """True if a game qualifies for mini-stacking via any of three paths.

    PATH 1 (blowout favorite, favored side only): the caller's `moneyline`
    must clear STACK_ELIGIBILITY_MONEYLINE AND O/U must clear
    STACK_ELIGIBILITY_VEGAS_TOTAL.  Caller is responsible for evaluating
    only the favored side.

    PATH 2 (extreme shootout, both sides eligible): O/U must clear
    STACK_ELIGIBILITY_SHOOTOUT_TOTAL — moneyline is ignored.  Caller may
    pass the favored team's moneyline (or either side); only the O/U
    threshold matters.

    PATH 3 (catastrophic opposing starter, favored side only): the
    opposing starter's season ERA must clear STACK_ELIGIBILITY_PATH3_OPP_SP_ERA
    AND the own team's season OPS must clear STACK_ELIGIBILITY_PATH3_OWN_TEAM_OPS.
    Both gates required — a bad SP only matters if the lineup can capitalize.
    Caller is responsible for evaluating only the side facing the bad SP.

    Unknown O/U returns False on PATH 1/2 (no fallback).  PATH 3 ignores O/U
    but requires both opp_starter_era and own_team_ops to be supplied.
    """
    if (opp_starter_era is not None and own_team_ops is not None
            and opp_starter_era >= STACK_ELIGIBILITY_PATH3_OPP_SP_ERA
            and own_team_ops >= STACK_ELIGIBILITY_PATH3_OWN_TEAM_OPS):
        return True
    if vegas_total is None:
        return False
    if vegas_total >= STACK_ELIGIBILITY_SHOOTOUT_TOTAL:
        return True
    if moneyline is None:
        return False
    return (
        moneyline <= STACK_ELIGIBILITY_MONEYLINE
        and vegas_total >= STACK_ELIGIBILITY_VEGAS_TOTAL
    )

# Environmental filter thresholds (Filter 2)
# Pitcher environmental pass conditions
PITCHER_ENV_WEAK_OPP_OPS = 0.700      # bottom-10 offense OPS threshold
PITCHER_ENV_MIN_K_PER_9 = 8.0         # min K/9 for "K upside"

# ---------------------------------------------------------------------------
# Bifurcated missing-data handling
# Strict-mode (May 2026): the DNP penalty machinery (DNP_RISK_PENALTY,
# DNP_UNKNOWN_PENALTY, ENV_UNKNOWN_COUNT_THRESHOLD) was removed.  The DNP
# filter (`is_player_scoreable` + the batting_order check) excludes any
# batter without a projected lineup spot or any pitcher without full season
# stats.  Every candidate the optimizer sees has full data, so the DNP
# adjustment factor is constant 1.0 and no penalty is needed.
# ---------------------------------------------------------------------------

# Env modifier bounds — PRIMARY EV signal.
# Range: 0.20–1.30 (batter), 0.20–1.55 (pitcher), 0.20–1.10 (rookie).
# Game conditions: Vegas O/U, ERA, bullpen, park, weather, platoon, batting
# order, moneyline.  Floor is shared across all three; ceiling is asymmetric
# per position (see PITCHER_ENV_MODIFIER_CEILING / ROOKIE_ENV_MODIFIER_CEILING).
ENV_MODIFIER_FLOOR = 0.20   # V12.1: lowered from 0.40 (which was already cut
                             # from V11 0.70).  Tuning sweep across 35-slate
                             # backtest showed lower floors improve mean
                             # slot-weighted RS without hurting beat-winner
                             # rate (steady at 51.4% from floor 0.10-0.40).
                             # 0.20 is the sweet spot: meaningful EV signal
                             # spread (env=0 → 0.20 multiplier, env=1 → 1.30/1.40),
                             # yet still floors a "no info" candidate with a
                             # base value (so the variant chooser doesn't try
                             # to fill slots with zero-EV stragglers).
# V15.3 (May 6, 2026): V15.2 batter ceiling tighten REVERTED.  V15.2
# moved batter ceiling 1.30 → 1.20 to let elite trait compete with
# saturated-env weak bats — but kept pitcher ceiling at 1.55, which
# WIDENED the pitcher-vs-batter asymmetry from 19% to 29% and made the
# 4P+1B variant systematically beat 0P+5B in the variant chooser.
# Result: post-V15.2 redeploy produced lineups with 4 pitchers + 1
# batter (e.g. 2026-05-06 Ragans/Cantillo/Soroka/Pérez/Clemens), where
# the platform actually caps lineups at 1 pitcher.  Reverted to 1.30
# concurrent with adding MAX_PITCHERS_PER_LINEUP = 1 below.
ENV_MODIFIER_CEILING = 1.30

# V10.6 (April 28-29 evaluation): pitcher-specific env ceiling, asymmetric.
# 33-slate harness analysis showed pitchers occupied 54% of model top-10 (target
# ~40% given ~50% of HV slots historically go to batters).  Root cause: pitcher
# env saturates at 1.0 too easily (5 factors × 1.0 + home 0.5 → 5.5/5.5 = 1.0)
# while batter env is soft-capped in Group A and unlikely to break 0.85.  Two
# pitchers each at env 0.95 + bare trait both clear 110 EV; the batter pool
# tops out around 100 EV → top-10 stuffed with starters.  Tightening the
# pitcher band lets exceptional batter env situations (Coors shootouts, weak
# bullpen blowups) compete with the dominant favorite-team SP.  The pitcher
# floor is unchanged at 0.70 — bad-env pitchers should still be priced out
# at the floor, just with a smaller upside cap.
# V12: pitcher env ceiling RAISED to 1.40 (was 1.20).  V10.6 dropped it to 1.20
# to fix saturation under the old env scoring.  V12 env scoring is harder to
# saturate (sparser signal contributions) AND empirical pitcher RS is 34% higher
# than batter RS (3.33 vs 2.49 mean across 35 slates).  Capping pitchers at
# 1.20 while batters max at 1.30 was structurally backward — pitchers should
# have HIGHER EV ceiling because their RS distribution is fatter.  Backtest
# shows 1.40 produces a healthier composition mix (closer to the empirical
# winning shapes: 2P+3B 28.6%, 0P+5B 25.7%, 1P+4B 17.1%) instead of the
# nearly-monoculture 0P+5B that 1.20 produced.
#
# V13 update: 38-slate audit shows pitcher mean RS is 1.38x batter mean RS
# (3.42 vs 2.48), and the V12 backtest under 1.40 ceiling produced too-few
# multi-pitcher lineups (1P+4B = 37% of optimizer output vs 17% of winners,
# 2P+3B = 23% of output vs 28.6% of winners).  Bumping ceiling to 1.55
# (~19% asymmetry vs 7.7% prior) should push the marginal cases toward
# 2P+ shapes that the audit shows actually win more often.
PITCHER_ENV_MODIFIER_CEILING = 1.55

# V13.3 (May 2026): rookie-track players have NO MLB quality signal — their
# trait_factor is fixed at 1.0 (neutral) by design, so EV is purely
# env-driven.  Without a separate ceiling they can saturate to the full
# pitcher (1.55) or batter (1.30) cap whenever env aligns — which is
# common for rookies because underdog teams are exactly where debutants
# get the ball, and V13's "underdog peak ML" reward saturates env for
# unproven pitchers in their typical context.  Cap rookie env just above
# neutral so unproven players cannot beat established trait-rated players
# on env alone.  Floor unchanged at ENV_MODIFIER_FLOOR (rookies in bad
# matchups still hit the floor).
ROOKIE_ENV_MODIFIER_CEILING = 1.10

# Trait modifier bounds — SECONDARY EV signal.
# V15.4 (May 6, 2026): widened 0.85–1.15 (1.35x swing) → 0.70–1.20 (1.71x swing).
# The 0.85 floor was too generous — a rating-9.6 batter (deep slump, sub-0.500
# OPS, no recent hits) still got 88% of an average hitter's EV under the old
# band, allowing him to beat better-rated hitters whenever env was modestly
# favorable.  Empirical OPS-by-quartile MP-rate signal in the historical
# corpus shows ~9× swing (Q1 0.65 OPS = 7% MP, Q4 0.95 OPS = 64% MP), so the
# 1.35x band was severely underweighting trait.  1.71x is still conservative
# vs the empirical signal but punishes near-zero-rated players harder:
#   rating  9.6 → trait_factor 0.748 (was 0.879, -15%)
#   rating 50   → trait_factor 0.950 (was 1.000)
#   rating 80   → trait_factor 1.100 (was 1.090)
#   rating 100  → trait_factor 1.200 (was 1.150)
# Net effect: dredge-tier hitters (rating <20) now drop ~15% on EV, letting
# average-or-better trait survive at neutral env.
TRAIT_MODIFIER_FLOOR = 0.70
TRAIT_MODIFIER_CEILING = 1.20

# Recent form volatility modifier — applies when recent_form CV is high.
# High variance (CV near 1.0) in recent production signals sensitivity to conditions.
# DFS payouts are convex (Highest Value leaderboard rewards tails, not medians);
# a volatile batter in a good matchup has more upside than a steady batter with
# the same mean.  Max amplification: 1.0 + (1.0 × 0.20) = 1.20x for highly
# volatile batters — still less than half the env swing (1.86×) so it cannot
# dominate ranking, but enough to meaningfully reorder boom-or-bust profiles.
BATTER_FORM_VOLATILITY_MAX = 0.20

# ---------------------------------------------------------------------------
# Lineup structure validation
# ---------------------------------------------------------------------------
# MAX_PLAYERS_PER_TEAM replaced by MAX_PLAYERS_PER_TEAM_BATTERS above.
# Stacking up to 4 teammates (all batter slots) is explicitly allowed.
# V12: pitcher count is unconstrained (0..5).  Slot 1 (2.0×) goes to the
# highest-EV player regardless of position (rearrangement inequality).
PITCHER_ANCHOR_SLOT = 1              # legacy constant — Slot 1 index, used in slot_assignment

# ---------------------------------------------------------------------------
# Blowout game stack bonus (4-term EV formula)
# Applied in _compute_base_ev() when a player's team is the favored side
# in a blowout game (moneyline <= BLOWOUT_MONEYLINE_THRESHOLD).
#
# V13.3 (May 2026): lowered 1.20 → 1.10 after a 40-slate manual audit of
# slot-1 winners showed 0/40 came from a stacked-team batter — the
# slot-1 winner is consistently a pitcher (62.5%) or an elite OF/DH
# (27.5%) on a non-stack team.  The 20% bonus was overweighting
# stack-eligible-team batters into top-EV without empirical support.
# 10% still recognises positive correlation upside without dominating
# the lineup vs non-stack elites.
# ---------------------------------------------------------------------------
STACK_BONUS = 1.10  # 10% EV bonus for players on blowout-game teams (V13.3)

# V13.3 (May 2026): position-volume multiplier for batters.  Catchers win
# 0% of slot-1 spots in 40 historical slates (rest days, pinch-hits,
# pulls — ~3.0 PAs/game vs 4.2 for OF).  2B/SS win 0% of slot-1 too
# (lower OPS distributions).  Apply a small structural haircut so an
# elite-OPS catcher cannot outscore a non-catcher with the same trait
# in the same env.  Default 1.0 for OF / 1B / 3B / DH / P (pitchers
# bypass this term — they have their own pitcher_ev_ceiling).
# Reads via .get(position.upper(), 1.0) so unknown positions default to
# no penalty rather than crashing.
POSITION_VOLUME_MULTIPLIER = {
    "C": 0.90,
    "2B": 0.95,
    "SS": 0.95,
}

# ---------------------------------------------------------------------------
# Strict-mode (May 2026): no league-average DEFAULT_* fallbacks.  The pipeline
# crashes loud when any required live signal is missing.  Historical
# DEFAULT_OPP_OPS / DEFAULT_OPP_K_PCT / DEFAULT_PITCHER_ERA / DEFAULT_PITCHER_WHIP
# / DEFAULT_BATTER_OPS_VS_LHP / DEFAULT_BATTER_OPS_VS_RHP constants were
# removed — they were dead code by V12 and accommodating them at all violated
# the no-fallback policy.

# ---------------------------------------------------------------------------
# V12 env scoring constants
#
# V12 simplified: thresholds for the strong audit-validated signals are inline
# in compute_pitcher_env_score / compute_batter_env_score for clarity.  Only
# the wind-direction sets remain here because they're shared across many
# downstream loaders.
# ---------------------------------------------------------------------------
BATTER_ENV_WIND_OUT_DIRECTIONS = ("OUT",)
BATTER_ENV_WIND_IN_DIRECTIONS = ("IN",)

# ---------------------------------------------------------------------------
# Game status constants
# Games in these statuses will never receive scores; treat as "done" so the
# post-lock monitor and cache completion check don't perma-freeze.
# ---------------------------------------------------------------------------
NON_PLAYING_GAME_STATUSES = frozenset({"Postponed", "Cancelled", "Suspended"})

# Games that have already started (in-progress or completed). The T-65
# pipeline filters these out of every enrichment, scoring, and candidate-pool
# stage so a mid-slate app redeploy (slate already active) runs cold on the
# remaining games only — the Odds API does not return lines for started games,
# so re-enriching them would crash the pipeline.
STARTED_GAME_STATUSES = frozenset({"Live", "Final"})


def is_game_remaining(game_status: str | None) -> bool:
    """True if the game hasn't started. Null status = safe default (remaining)."""
    return game_status not in STARTED_GAME_STATUSES

# Scoring engine scaling (K/9 shared between scoring_engine and filter_strategy)
SCORING_K9_FLOOR = 6.0                # K/9 at or below → 0 pts
SCORING_K9_CEILING = 12.0             # K/9 at or above → max pts

# UNKNOWN_SCORE_RATIO removed in May-2026 strict pass.  Trait scorers now
# raise RuntimeError instead of returning a "neutral" 0.5 × max_pts, since
# the upstream DNP filter guarantees every scored player has full live data.

# Scoring engine — pitcher matchup thresholds
SCORING_PITCHER_OPS_CEILING = 0.800   # opponent OPS at or above → 0 score
SCORING_PITCHER_OPS_RANGE = 0.150     # OPS scoring range
SCORING_PITCHER_K_PCT_FLOOR = 0.18    # opponent K% at or below → 0 score
SCORING_PITCHER_K_PCT_RANGE = 0.10    # K% scoring range

# Scoring engine — pitcher ERA/WHIP thresholds
SCORING_ERA_CEILING = 5.0             # ERA at or above → 0 score
SCORING_ERA_RANGE = 3.0               # ERA scoring range (5.0 - 2.0)
SCORING_WHIP_CEILING = 1.5            # WHIP at or above → 0 score
SCORING_WHIP_RANGE = 0.6              # WHIP scoring range (1.5 - 0.9)

# Scoring engine — batter matchup thresholds
SCORING_BATTER_ERA_FLOOR = 2.5        # opposing ERA at or below → 0 score
SCORING_BATTER_ERA_RANGE = 2.5        # ERA scoring range (5.0 - 2.5)
SCORING_BATTER_WHIP_FLOOR = 0.9       # opposing WHIP at or below → 0 score
SCORING_BATTER_WHIP_RANGE = 0.6       # WHIP scoring range (1.5 - 0.9)

# Scoring engine — batter OPS-split matchup thresholds (handedness-specific)
# When starter_hand and batter splits are known, blended into matchup score.
SCORING_BATTER_OPS_SPLIT_FLOOR = 0.600   # batter OPS-vs-hand at or below → 0 split score
SCORING_BATTER_OPS_SPLIT_RANGE = 0.300   # range for full split score (0.600 → 0.900)

# V10.6 (Apr 28-29 evaluation, follow-up): batter K-vulnerability signal.
# Closes the floor-risk gap surfaced by user feedback on the eval results:
# even with V10.6 env-side opp K/9 (Group A6), the model can still recommend
# a high-strikeout batter (e.g., Joey Gallo, Jake Burger, Schwarber) into a
# matchup vs an elite K-pitcher and miss the obvious 0-for-4-with-3K floor.
#
# Mechanism: the BATTER's own season K% (so / max(pa, 1)) is combined with
# the OPPOSING starter's K/9 to produce a "double-jeopardy" sub-signal.
# Both have to be high for the penalty to fully fire — a contact-oriented
# hitter (low K%) is fine vs an elite K-pitcher because their bat-to-ball
# floor protects them; a high-K hitter is fine vs a contact pitcher because
# the pitcher won't generate the whiffs to bury him.  Only the cross
# (high × high) is the dangerous combination.
#
# Wired into score_batter_matchup() as a 4th sub-signal at 15% weight (era
# 35%, whip 20%, hand-split 30%, k-vuln 15%).  Anti-aligned with the
# Group A6 env signal: env scores the OPPORTUNITY for runs (via opp K/9
# alone), trait scores the FLOOR for an individual batter (batter K% × opp
# K/9).  Two distinct signals, naturally redundant when both lit.
#
# Floor=0.18 (elite contact, e.g., Arraez/Tucker tier) → no penalty.
# Ceiling=0.30 (high-whiff bat) → full sub-signal contribution to penalty.
SCORING_BATTER_K_PCT_FLOOR = 0.18         # batter K% at or below → 0 vulnerability score
SCORING_BATTER_K_PCT_CEILING = 0.30       # batter K% at or above → full vulnerability score
# K-vulnerability cross-axis: opposing-starter K/9 thresholds.
# These are tighter than the env A6 thresholds (10.5 / 6.5) because we
# want the trait penalty to fire on truly-elite K-arms only — Group A6
# already broadly de-rates batters in any high-K matchup at the env layer.
SCORING_OPP_K9_VULN_FLOOR = 7.5           # opp K/9 at or below → 0 cross contribution
SCORING_OPP_K9_VULN_CEILING = 11.0        # opp K/9 at or above → full cross contribution

# V10.8 — opposing-starter xwOBA-against thresholds for batter matchup_quality.
# This is the simplified pitch-arsenal-mismatch signal: a single number
# capturing how well the pitcher's overall arsenal suppresses contact
# quality, independent of BABIP / sequencing luck.  See research:
# https://baseballsavant.mlb.com/leaderboard/expected_statistics
#
# Floor 0.330 = league-average wOBA-against (no penalty), ceiling 0.265 =
# elite arsenal (full penalty for the opposing batter's matchup quality).
# The descending range mirrors the scale: lower xwOBA-against = better
# arsenal = worse for the batter.
SCORING_OPP_X_WOBA_AGAINST_FLOOR = 0.330      # at or above → 0 contribution (weak arsenal, batter favored)
SCORING_OPP_X_WOBA_AGAINST_CEILING = 0.265    # at or below → full contribution (elite arsenal)

# V10.8 — catcher framing adjustment to pitcher k_rate trait.
#
# Research (TruMedia framing model, FanGraphs 2026 catcher rankings): each
# framing run per game adds ~3.9% to a pitcher's K rate and subtracts ~3.9%
# from his BB rate.  Top team catcher framing aggregates run ~10-15 runs/season
# (over ~22,000 called pitches), so the per-pitch impact is small but real.
#
# Under the 2026 ABS Challenge System, catcher framing's predictive value is
# REDUCED — challenges fix the worst calls — but the system still calls
# ~98% of pitches via human umpires (per ESPN/MLB ABS announcement, only
# 2 challenges per team per game).
#
# V13 update: 38-slate audit of own-team framing_runs vs pitcher HV-rate
# shows Q4 (top framers, framing_runs ≥ +1.06) HV=40.0% vs Q1 (bottom
# framers, ≤-0.83) HV=21.2% — a +18.8pp swing.  V10.8 capped the trait
# adjustment at ±5%, which translated to ~0.5% EV change after passing
# through k_rate (35/100 trait weight) → trait_factor (0.85-1.15).  That
# was structurally too small for an 18.8pp HV signal.  Bumping to ±12%
# triples the effective EV impact while staying conservative against the
# ABS-era reduction in framing's pitch-by-pitch effect.
#
# `framing_runs_floor` / `_ceiling`: the team-level framing_runs values
# that map to ±max adjustment.  A team at +12 runs/season gets +12% on k_rate;
# a team at -12 runs/season gets -12%.  Mid-pack teams (~0 runs) → no change.
SCORING_FRAMING_RUNS_CEILING = 12.0           # at or above → +max k_rate adjustment
SCORING_FRAMING_RUNS_FLOOR = -12.0            # at or below → -max k_rate adjustment
SCORING_FRAMING_K_RATE_MAX_ADJ = 0.12         # V13: ±12% (was ±5%)
                                              # (deliberately conservative for ABS era)

# Scoring engine — park factor range boundaries (LAD floor, COL ceiling)
# Used in score_ballpark_factor() to normalise the effective park factor.
PARK_HR_FACTOR_MIN = 0.89             # lowest value in PARK_HR_FACTORS (LAD)
PARK_HR_FACTOR_MAX = 1.38             # highest value in PARK_HR_FACTORS (COL)

# Slate classification — quality-SP matchup ERA threshold.
# A starter with ERA below this is eligible to be counted as a "quality SP"
# when paired with a weak opposing lineup (OPS or K/9 gate).
# Used by classify_slate() in filter_strategy.py.
QUALITY_SP_ERA_THRESHOLD = 3.5

# Scoring engine — power profile component maxima and target denominator.
# V10.0: rebalanced to reflect the strategy-doc hierarchy — average exit
# velocity and hard-hit % are the ground-truth signals; ISO/HR are outcome
# proxies that lag.  When Statcast data is present, it dominates.
# V13.1 — `score_offensive_profile` (formerly score_power_profile).  The
# 40-pt batter trait was rebuilt around season OPS as the holistic anchor
# instead of pure Statcast kinematics.  A high-OPS contact-and-on-base
# hitter (Arraez, Freeman) used to score ~6/40 because the trait only saw
# exit-velo / hard-hit / barrels — none of which fire for singles+walks
# profiles.  With OPS as the dominant sub-signal (10 of 30 sub-pts), those
# hitters now reach the EV pool on their own merit when matchup conditions
# favor them — no diversity quota, no categorical contact-vs-power slicer.
#
# Sub-signal weights (out of OFFENSIVE_PROFILE_DENOM = 30 sub-pts, scaled
# to the trait's 40 max_pts inside `score_offensive_profile`):
#   OPS         (anchor)               → 10 sub-pts  (NEW V13.1)
#   x_woba                              →  7 sub-pts  (bumped from 4)
#   hard_hit_pct                        →  5 sub-pts  (trimmed from 7)
#   barrel_pct                          →  4 sub-pts  (trimmed from 6)
#   avg_exit_velocity                   →  4 sub-pts  (trimmed from 7)
#   max_exit_velocity                   →  0 sub-pts  (DROPPED — 1pt of noise,
#                                                     xwOBA + barrel cover the
#                                                     power-tail confirmation)
#
# OPS floor 0.700 / ceiling 0.950.  League-average regular OPS is ~0.730;
# 0.950+ is the Judge/Soto tier.  ISO is NOT used as a sub-signal — OPS
# already includes SLG, and adding ISO would double-count power exactly
# in the direction we're trying to balance against.  ISO continues to be
# computed and stored on PlayerStats for historical reference only.
OFFENSIVE_PROFILE_OPS_FLOOR = 0.700
OFFENSIVE_PROFILE_OPS_CEILING = 0.950
POWER_PROFILE_BARREL_PCT_MAX = 15.0
POWER_PROFILE_AVG_EV_MAX = 92.0
POWER_PROFILE_HARD_HIT_MAX = 50.0
POWER_PROFILE_AVG_EV_FLOOR = 85.0
POWER_PROFILE_HARD_HIT_FLOOR = 30.0
POWER_PROFILE_BARREL_PCT_FLOOR = 4.0

# V10.8 — batter xwOBA scaling.  xwOBA derives from exit velocity + launch
# angle (and sprint speed for some batted balls); it captures the full
# value of contact, including weak grounders and warning-track flies that
# don't show up in HR counts.  See MLB Glossary on xwOBA:
# https://www.mlb.com/glossary/statcast/expected-woba
#
# Floor 0.300 = league-average wOBA (no credit), ceiling 0.400 = elite
# (Judge / Soto / Ohtani tier, full credit).
POWER_PROFILE_X_WOBA_FLOOR = 0.300        # at or below → 0 contribution
POWER_PROFILE_X_WOBA_CEILING = 0.400      # at or above → full contribution

# V10.8 — pitcher xERA scaling for era_whip trait.  xERA is a 1:1 conversion
# of xwOBA-against onto the ERA scale; widely-used in DFS to flag regression
# candidates (live ERA shiny but xERA bad → coming back to earth).  See
# Pitcher K/9 kinematics — Statcast-driven version of k_rate scoring.
# Strategy doc §"Kinematics of the Pitching Anchor": K/9 is downstream;
# the upstream physics are FB velocity, induced vertical break, extension
# (perceived velo), whiff %, and chase %.  When Statcast is available, a
# blended kinematic score replaces raw K/9 because the physics are predictive
# while K/9 is retrospective.
SCORING_FB_VELOCITY_FLOOR = 92.0     # mph — league-avg four-seam
SCORING_FB_VELOCITY_CEILING = 99.0   # mph — elite velocity tier
SCORING_FB_IVB_FLOOR = 13.0          # inches — below this = flat fastball
SCORING_FB_IVB_CEILING = 19.0        # inches — elite ride (Schlittler/Abel tier)
SCORING_FB_EXTENSION_FLOOR = 5.8     # feet — short release
SCORING_FB_EXTENSION_CEILING = 7.0   # feet — elite perceived-velo gain
SCORING_WHIFF_PCT_FLOOR = 20.0       # %
SCORING_WHIFF_PCT_CEILING = 35.0     # % — elite swing-and-miss
SCORING_CHASE_PCT_FLOOR = 24.0       # %
SCORING_CHASE_PCT_CEILING = 38.0     # % — elite o-swing generator

# ET → UTC offset used to derive the weather-lookup hour from a game's
# ET clock time.  Regular season is entirely on EDT (UTC-4), so this is
# correct for every MLB regular-season game.  Does NOT handle DST edge
# cases outside the season window — MLB scheduling guarantees regular
# season starts after the spring-forward transition and ends before fall-back.
ET_TO_UTC_OFFSET_HOURS = 4

# ---------------------------------------------------------------------------
# Leverage / contrarian-edge scoring (V14, May 2026 — STRATEGY_AUDIT_2026-05.md)
#
# The pre-V14 EV ranking was popularity-blind: two players with identical
# env+trait scores were ranked interchangeably regardless of whether one
# was on every entrant's roster and the other was overlooked.  The 40-slate
# audit shows 92.5% of winning Real Sports lineups contained at least one
# Highest Value player who was NOT on the Most Popular leaderboard, and
# popular HVs and sleeper HVs score essentially identically (mean RS 4.44
# vs 4.39).  Performance-prediction parity, ownership disparity → leverage
# is where the contest-winning edge lives.
#
# The leverage signal is computed from a deterministic, rule-based predictor
# (app/core/popularity.py) that consumes only publicly-observable pre-game
# inputs (team market, season-to-date star status, batting order, current
# stats, rolling 14-day Most Popular history).  It does NOT consume raw
# historical `drafts` counts, `card_boost`, `total_value`, or any RS outcome
# label.  See app/core/popularity.py for the model and the audit doc for
# the empirical case.
# ---------------------------------------------------------------------------

# V15 — Continuous popularity-score → EV multiplier.
#
# Replaces V14's discrete bucket system (top_decile / upper_mid / mid /
# lower_mid / bottom_decile).  Calibrated by scripts/calibrate_popularity_curve.py
# against the 28+ slate historical_players.csv corpus (May 2026).
#
# Linear curve, clamped to [FLOOR, CEILING]:
#   multiplier(score) = clamp(1.0 + (NEUTRAL - score) * SLOPE, FLOOR, CEILING)
#
# Empirical signal in the corpus is extreme — score-0 players HV at 71.9%
# vs score-8 players at 18.6% (alpha ratio ~30×).  We deliberately keep
# the multiplier band narrow [0.85, 1.20] (1.41× swing) so leverage stays
# a tiebreaker, not an override of env (which has 6.5–7.75× swing).
#
# NEUTRAL_SCORE = pool weighted-mean popularity score (rounded).
# SLOPE = avg of (1−FLOOR)/(p90−NEUTRAL) and (CEILING−1)/(NEUTRAL−p10) on
#         the corpus, so the curve naturally saturates near the tails.
# V15.1 (May 2026) — re-fitted constants after replacing V15's binary
# threshold components with continuous ramps.  V15.1 scores have wider
# spread (typical max ~9.0 vs V15's ~7.5) because elite-stats and
# fame-rate now contribute proportionally rather than capped at +2/+1
# steps.  Re-fitting NEUTRAL and SLOPE preserves the original intent
# (multiplier saturates near pool tails, neutral at pool mean) on the
# new score distribution.
#
# Curve preview at calibrated constants (see scripts/calibrate_popularity_curve.py):
#   score 0 → mult 1.25 (CEILING / max sleeper boost; HV-rate 66%)
#   score 4.5 → mult 1.00 (neutral; HV-rate 49%)
#   score 9 → mult 0.80 (FLOOR / max consensus discount; HV-rate 16%)
POPULARITY_NEUTRAL_SCORE = 4.5
POPULARITY_SLOPE = 0.09
POPULARITY_MULT_FLOOR = 0.80
POPULARITY_MULT_CEILING = 1.40

# Team market tier — drives one of four families of popularity features.
# Tier 1 = national following (top of every casual fan's mind), tier 4 =
# low-draft-volume markets the field consistently overlooks.  Sourced from
# public MLB market-size and TV-rights distribution data, NOT from the
# historical drafts column.  Static; updated once per offseason.
TEAM_MARKET_TIER = {
    # Tier 1 — national-television regulars, premium markets
    "NYY": 1, "LAD": 1, "BOS": 1, "CHC": 1, "PHI": 1, "NYM": 1,
    # Tier 2 — large markets, strong regional followings
    "ATL": 2, "STL": 2, "SF": 2, "HOU": 2, "TOR": 2, "SD": 2, "SEA": 2,
    # Tier 3 — mid-market
    "TEX": 3, "MIN": 3, "BAL": 3, "CLE": 3, "MIL": 3, "DET": 3,
    "ARI": 3, "CIN": 3, "WSH": 3, "LAA": 3,
    # Tier 4 — small-market, persistently under-drafted
    "KC": 4, "PIT": 4, "MIA": 4, "ATH": 4, "COL": 4, "TB": 4, "CWS": 4,
}

# Star-player allowlist — players whose name recognition consistently drives
# ownership independent of current-slate matchup signals.  Sourced from
# 2025 All-Star rosters, MVP/Cy Young top-5 voting, and Silver Slugger /
# Gold Glove winners.  NOT sourced from the drafts column.  Updated once
# per offseason.  Names stored in the same normalised form used by
# app.core.utils.find_player_by_name (accent-stripped, lowercased).
STAR_PLAYER_FLAGS = frozenset({
    # 2025 MVP top-5 (each league)
    "aaron judge", "shohei ohtani", "jose ramirez", "bobby witt jr",
    "juan soto", "freddie freeman", "francisco lindor", "mookie betts",
    "ketel marte", "yordan alvarez",
    # 2025 Cy Young top-5
    "tarik skubal", "garrett crochet", "paul skenes", "chris sale",
    "zack wheeler", "logan webb", "tyler glasnow", "blake snell",
    "yoshinobu yamamoto", "max fried",
    # Returning stars / household names (multi-time All-Stars, perennial fame)
    "mike trout", "manny machado", "fernando tatis jr", "ronald acuna jr",
    "vladimir guerrero jr", "rafael devers", "kyle tucker", "corey seager",
    "bryce harper", "trea turner", "carlos correa", "jose altuve",
    "matt olson", "pete alonso", "alex bregman", "anthony rizzo",
    "salvador perez", "nolan arenado", "marcell ozuna", "william contreras",
    "gunnar henderson", "elly de la cruz", "wyatt langford",
    "jackson chourio", "jackson holliday", "jackson merrill",
    # Pitchers with crossover fame
    "spencer strider", "jacob degrom", "shane mcclanahan", "corbin burnes",
    "kevin gausman", "freddy peralta", "framber valdez", "george kirby",
    "logan gilbert", "dylan cease", "joe ryan", "ranger suarez",
    "aaron nola", "sonny gray", "justin verlander", "clayton kershaw",
})

# V15.1 (May 2026) — continuous fame index, position-aware window.
# Replaces V14/V15's binary thresholds (>=1 → +1, >=3 → +2) with a
# rate-based signal: mp_appearances / total_appearances over the trailing
# window, scaled to [0, LEVERAGE_FAME_RATE_MAX_PTS].  Calibrated by
# scripts/calibrate_popularity_components.py against actual MP-flag
# outcomes — the rate-based signal lifted batter MP-prediction AUC from
# 0.823 → 0.848 by capturing the gradient between "popular every start"
# and "popular once in 14 days" the binary thresholds collapsed to +1 each.
#
# Position-aware windows: starting pitchers pitch every 5 days, so a 14-day
# window has at most 3 starts as denominator; lengthening to 28 days gives
# 5-6 starts and a stable rate.  Batters appear ~5 of every 7 days, so 14
# days is already 8-10 appearances and stable.
LEVERAGE_FAME_INDEX_DAYS_BATTER = 14
LEVERAGE_FAME_INDEX_DAYS_PITCHER = 28
LEVERAGE_FAME_RATE_MAX_PTS = 3.0

# Backwards-compat alias — used only by scripts/calibrate_popularity_curve.py
# (the score → multiplier curve fitter, runs offline).  Live runtime uses
# the position-aware constants above.
LEVERAGE_FAME_INDEX_DAYS = LEVERAGE_FAME_INDEX_DAYS_BATTER

# V15.1 — continuous elite-stats signal, replaces V14/V15's binary thresholds
# (OPS >= 0.900 → +2 batter, ERA <= 3.00 → +2 pitcher).  Linear scale within
# [floor, ceiling], maxing at LEVERAGE_ELITE_STAT_MAX_PTS (held at 2.5 so a
# flagged star at +3 still slightly outranks pure-elite-stats at +2.5).
#
# Floor/ceiling endpoints calibrated to where the empirical MP-rate ramp
# starts and saturates in historical_players.csv:
#   * Batters: OPS 0.65 → 7% MP-rate; OPS 0.95 → 64% MP-rate.  The 0.50–0.65
#     band is also low-MP but at smaller N and includes utility bats with
#     limited PAs; using 0.65 as the floor avoids over-weighting that noise.
#   * Pitchers: ERA <= 4.00 → ~80% MP-rate (the "draft-relevant tier"); ERA
#     >= 4.50 falls to 50–70% MP-rate with high variance.  Setting ceiling
#     at 4.50 gives partial credit through the entire usable starter band
#     and zero credit only past the bullpen-tier line.
LEVERAGE_ELITE_BATTER_OPS_FLOOR = 0.650
LEVERAGE_ELITE_BATTER_OPS_CEILING = 0.950
LEVERAGE_ELITE_PITCHER_ERA_FLOOR = 2.50
LEVERAGE_ELITE_PITCHER_ERA_CEILING = 4.50
LEVERAGE_ELITE_STAT_MAX_PTS = 2.5

# Backwards-compat aliases — used only by scripts/calibrate_popularity_curve.py
# (the score → multiplier curve fitter).  Live runtime uses the floor/ceiling
# pairs above.  Aliased to the legacy values so calibrate_popularity_curve.py
# can still recompute the V15 score for comparison reporting.
LEVERAGE_STAR_BATTER_OPS = LEVERAGE_ELITE_BATTER_OPS_CEILING
LEVERAGE_STAR_PITCHER_ERA = LEVERAGE_ELITE_PITCHER_ERA_FLOOR


# ---------------------------------------------------------------------------
# Startup self-check: validate that all scoring constants are in sensible ranges.
# This runs once at import time (cheap, all in-memory) and raises AssertionError
# loudly if a constant edit produces an incoherent configuration — e.g., a
# floor set above its ceiling, or an env modifier inverted.
# ---------------------------------------------------------------------------

def _validate_constants() -> None:
    # Env modifier band must be ascending and centred around 1.0
    assert ENV_MODIFIER_FLOOR < 1.0 < ENV_MODIFIER_CEILING, (
        f"ENV_MODIFIER band must straddle 1.0: [{ENV_MODIFIER_FLOOR}, {ENV_MODIFIER_CEILING}]"
    )
    assert TRAIT_MODIFIER_FLOOR < 1.0 < TRAIT_MODIFIER_CEILING, (
        f"TRAIT_MODIFIER band must straddle 1.0: [{TRAIT_MODIFIER_FLOOR}, {TRAIT_MODIFIER_CEILING}]"
    )

    # V12: pitcher env ceiling can EXCEED batter ceiling because pitcher RS
    # distribution is empirically fatter.  Floor still applies.
    assert ENV_MODIFIER_FLOOR < PITCHER_ENV_MODIFIER_CEILING, (
        f"PITCHER_ENV_MODIFIER_CEILING ({PITCHER_ENV_MODIFIER_CEILING}) must satisfy "
        f"FLOOR ({ENV_MODIFIER_FLOOR}) < this"
    )

    # V12: env-scoring threshold validations were removed along with their
    # constants — env score is now built from inline thresholds in
    # compute_pitcher_env_score / compute_batter_env_score.  See those
    # functions for the active V12 thresholds and audit citations.

    # Scoring thresholds
    assert SCORING_K9_FLOOR < SCORING_K9_CEILING, (
        f"SCORING_K9: floor ({SCORING_K9_FLOOR}) must be < ceiling ({SCORING_K9_CEILING})"
    )
    assert SCORING_ERA_RANGE > 0, f"SCORING_ERA_RANGE must be positive: {SCORING_ERA_RANGE}"
    assert SCORING_WHIP_RANGE > 0, f"SCORING_WHIP_RANGE must be positive: {SCORING_WHIP_RANGE}"
    assert SCORING_FB_VELOCITY_FLOOR < SCORING_FB_VELOCITY_CEILING, (
        f"SCORING_FB_VELOCITY: floor ({SCORING_FB_VELOCITY_FLOOR}) must be < ceiling ({SCORING_FB_VELOCITY_CEILING})"
    )
    assert SCORING_FB_IVB_FLOOR < SCORING_FB_IVB_CEILING, (
        f"SCORING_FB_IVB: floor ({SCORING_FB_IVB_FLOOR}) must be < ceiling ({SCORING_FB_IVB_CEILING})"
    )

    # Slot multipliers must sum to a positive total and Slot 1 must equal
    # BASE_MULTIPLIER (the formula's slot-1 anchor — used by
    # _lineup_total_ev to slot-weight variants).  Drift here would break
    # the EV math silently.
    from app.core.utils import BASE_MULTIPLIER
    assert sum(SLOT_MULTIPLIERS.values()) > 0, "SLOT_MULTIPLIERS must have positive values"
    assert SLOT_MULTIPLIERS[1] > SLOT_MULTIPLIERS[5], "Slot 1 must have the highest multiplier"
    assert SLOT_MULTIPLIERS[1] == BASE_MULTIPLIER, (
        f"SLOT_MULTIPLIERS[1]={SLOT_MULTIPLIERS[1]} must equal BASE_MULTIPLIER={BASE_MULTIPLIER}"
    )

    # Park factors: must have at least one entry; COL should be the highest
    assert len(PARK_HR_FACTORS) >= 30, "PARK_HR_FACTORS missing teams"
    assert PARK_HR_FACTOR_MIN < 1.0 < PARK_HR_FACTOR_MAX, (
        f"PARK_HR_FACTOR range must straddle 1.0: [{PARK_HR_FACTOR_MIN}, {PARK_HR_FACTOR_MAX}]"
    )

    # Stacking thresholds: shootout total must be higher than the PATH 1 total
    assert STACK_ELIGIBILITY_SHOOTOUT_TOTAL > STACK_ELIGIBILITY_VEGAS_TOTAL, (
        f"Shootout total ({STACK_ELIGIBILITY_SHOOTOUT_TOTAL}) must exceed PATH 1 total ({STACK_ELIGIBILITY_VEGAS_TOTAL})"
    )

    # V15 popularity-curve band must straddle 1.0, slope must be positive,
    # and clamps must be ordered.  A miscalibration here silently disables
    # contrarian ranking or inverts it.
    assert POPULARITY_MULT_FLOOR < 1.0 < POPULARITY_MULT_CEILING, (
        f"POPULARITY_MULT band must straddle 1.0: "
        f"[{POPULARITY_MULT_FLOOR}, {POPULARITY_MULT_CEILING}]"
    )
    assert POPULARITY_SLOPE > 0, (
        f"POPULARITY_SLOPE must be positive (higher score → lower multiplier): "
        f"{POPULARITY_SLOPE}"
    )
    assert POPULARITY_NEUTRAL_SCORE > 0, (
        f"POPULARITY_NEUTRAL_SCORE must be positive: {POPULARITY_NEUTRAL_SCORE}"
    )
    # Every team in PARK_HR_FACTORS must have a market tier — missing a
    # team would silently default that team's players to 'mid' bucket and
    # mute the leverage signal for them.
    missing_tier = set(PARK_HR_FACTORS.keys()) - set(TEAM_MARKET_TIER.keys())
    assert not missing_tier, (
        f"TEAM_MARKET_TIER missing teams present in PARK_HR_FACTORS: {sorted(missing_tier)}"
    )

    # V15.1 — continuous component bounds.  Floor/ceiling must be ordered
    # in the right direction (OPS ascending = higher = more popular; ERA
    # descending = lower = more popular).  Inverting either silently flips
    # the contrarian signal, scoring the wrong tail of the distribution.
    assert LEVERAGE_ELITE_BATTER_OPS_FLOOR < LEVERAGE_ELITE_BATTER_OPS_CEILING, (
        f"OPS floor must be below ceiling (higher OPS = more popular): "
        f"[{LEVERAGE_ELITE_BATTER_OPS_FLOOR}, {LEVERAGE_ELITE_BATTER_OPS_CEILING}]"
    )
    assert LEVERAGE_ELITE_PITCHER_ERA_FLOOR < LEVERAGE_ELITE_PITCHER_ERA_CEILING, (
        f"ERA floor must be below ceiling (lower ERA = more popular, but "
        f"floor < ceiling on the value axis): "
        f"[{LEVERAGE_ELITE_PITCHER_ERA_FLOOR}, {LEVERAGE_ELITE_PITCHER_ERA_CEILING}]"
    )
    assert LEVERAGE_ELITE_STAT_MAX_PTS > 0, (
        f"LEVERAGE_ELITE_STAT_MAX_PTS must be positive: {LEVERAGE_ELITE_STAT_MAX_PTS}"
    )
    assert LEVERAGE_FAME_RATE_MAX_PTS > 0, (
        f"LEVERAGE_FAME_RATE_MAX_PTS must be positive: {LEVERAGE_FAME_RATE_MAX_PTS}"
    )
    assert LEVERAGE_FAME_INDEX_DAYS_BATTER > 0 and LEVERAGE_FAME_INDEX_DAYS_PITCHER > 0, (
        f"Fame-index windows must be positive: "
        f"batter={LEVERAGE_FAME_INDEX_DAYS_BATTER}, "
        f"pitcher={LEVERAGE_FAME_INDEX_DAYS_PITCHER}"
    )
    assert LEVERAGE_FAME_INDEX_DAYS_PITCHER >= LEVERAGE_FAME_INDEX_DAYS_BATTER, (
        f"Pitcher fame window should be at least as long as batter window "
        f"(pitchers play less frequently): batter="
        f"{LEVERAGE_FAME_INDEX_DAYS_BATTER}, pitcher="
        f"{LEVERAGE_FAME_INDEX_DAYS_PITCHER}"
    )


_validate_constants()
