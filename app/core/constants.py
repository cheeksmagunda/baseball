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
# A team is stack-eligible if its game satisfies EITHER:
#
#   PATH 1 (BLOWOUT FAVORITE — favored side only):
#     moneyline ≤ STACK_ELIGIBILITY_MONEYLINE  AND
#     Vegas O/U ≥ STACK_ELIGIBILITY_VEGAS_TOTAL
#
#   PATH 2 (EXTREME SHOOTOUT — both sides eligible):
#     Vegas O/U ≥ STACK_ELIGIBILITY_SHOOTOUT_TOTAL
#
# PATH 1 captures predictable blowouts (favorite scores; opposing pitcher
# shelled).  PATH 2 captures Coors-class shootouts where both lineups
# project to feast regardless of which side wins — those games are
# "glaringly obvious" run environments where mini-stacking either side
# is well-supported by Vegas.
#
# All other teams fall back to the one-batter-per-team default.  A heavy
# favorite in a low-scoring pitcher's duel (-220 with O/U 7.0) is NOT
# stack-eligible — fails both paths.
STACK_ELIGIBILITY_MONEYLINE = -200     # favorite threshold (PATH 1)
STACK_ELIGIBILITY_VEGAS_TOTAL = 9.0    # min O/U paired with ML in PATH 1
STACK_ELIGIBILITY_SHOOTOUT_TOTAL = 10.5  # min O/U for ML-agnostic shootout PATH 2

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

MIN_GAMES_REPRESENTED = 2        # pipeline-level data-sufficiency guard (not a lineup rule)


def is_stack_eligible_game(
    moneyline: int | None, vegas_total: float | None
) -> bool:
    """True if a game qualifies for mini-stacking via either path.

    PATH 1 (blowout favorite, favored side only): the caller's `moneyline`
    must clear STACK_ELIGIBILITY_MONEYLINE AND O/U must clear
    STACK_ELIGIBILITY_VEGAS_TOTAL.  Caller is responsible for evaluating
    only the favored side.

    PATH 2 (extreme shootout, both sides eligible): O/U must clear
    STACK_ELIGIBILITY_SHOOTOUT_TOTAL — moneyline is ignored.  Caller may
    pass the favored team's moneyline (or either side); only the O/U
    threshold matters.

    Unknown O/U returns False (no fallback).  Unknown moneyline only
    fails PATH 1; PATH 2 still evaluates O/U-only.
    """
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
#
# "Unknown environment" (missing data) ≠ "Bad environment" (confirmed bad).
# In DFS with convex payouts, uncertainty widens variance without shifting
# the mean.  A ghost player's missing batting order could be leadoff or DNP —
# penalizing as if it's DNP is asymmetrically wrong for high-boost players
# where any positive outcome crosses the threshold.
#
# Two tiers (see _compute_dnp_adjustment() in filter_strategy.py):
#   CONFIRMED_BAD: batting_order=None AND the player's team's lineup is
#                  published (so absence = genuinely not starting).
#   UNKNOWN:       batting_order=None AND lineup not yet published.
#                  Applies a lighter penalty reflecting true uncertainty.
# ---------------------------------------------------------------------------
DNP_RISK_PENALTY = 0.70               # CONFIRMED bad: 30% haircut (lineup published, player absent)
DNP_UNKNOWN_PENALTY = 0.93            # UNKNOWN: 7% haircut.
                                      # V10.6 (April 28-29 evaluation): lifted from
                                      # 0.85 → 0.93.  When the constant was set,
                                      # batting orders were rare at T-65 and the
                                      # 15% haircut reflected genuine DNP uncertainty.
                                      # V10.4 wired RotoWire expected-lineup scraping
                                      # which covers ~90% of teams at T-65, so the
                                      # batting_order=None state now correlates much
                                      # more with "RotoWire missed this team" than
                                      # with "this batter isn't starting".  The harness
                                      # showed batters were systematically out-ranked
                                      # by the dominant pitcher pool — every batter
                                      # paid 0.85 even when env conditions were strong.
                                      # Reducing the haircut to 7% lets confirmed-team
                                      # batters in good env situations compete on EV
                                      # with the favorite-team SP.  CONFIRMED_BAD
                                      # (lineup published, player absent) remains
                                      # at 0.70 — that's still a real signal.
ENV_UNKNOWN_COUNT_THRESHOLD = 3       # >= this many unknown env factors = "data not published" (not "bad env")

# Env modifier bounds — PRIMARY EV signal.
# Range: 0.70–1.30 (1.86x swing) — game conditions (Vegas O/U, ERA, bullpen,
# park, weather, platoon, batting order, moneyline).
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
PITCHER_ENV_MODIFIER_CEILING = 1.40

# Trait modifier bounds — SECONDARY EV signal.
# Range: 0.85–1.15 (1.35x swing) — season stats (K/9, ISO, barrel%, ERA, WHIP,
# recent form) provide fine-grained differentiation within the same env tier.
TRAIT_MODIFIER_FLOOR = 0.85
TRAIT_MODIFIER_CEILING = 1.15

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
# Applied in _compute_filter_ev() when a player's team is the favored side
# in a blowout game (moneyline <= BLOWOUT_MONEYLINE_THRESHOLD).
# ---------------------------------------------------------------------------
STACK_BONUS = 1.20  # 20% EV bonus for players on blowout-game teams

# ---------------------------------------------------------------------------
# League-average defaults for missing opponent / pitcher stats
#
# When a stat is None (not fetched or unavailable), scoring uses these
# league-average baselines.  A single constant controls each value so
# recalibrating for a new season only requires one change.
# ---------------------------------------------------------------------------
DEFAULT_OPP_OPS = 0.730               # 2026 league-average team OPS
DEFAULT_OPP_K_PCT = 0.22              # 2026 league-average team K%
DEFAULT_PITCHER_ERA = 5.0             # league-worst-tier ERA (conservative)
DEFAULT_PITCHER_WHIP = 1.5            # league-worst-tier WHIP (conservative)
DEFAULT_BATTER_OPS_VS_LHP = 0.720     # league-average batter OPS vs left-handed pitchers
DEFAULT_BATTER_OPS_VS_RHP = 0.740     # league-average batter OPS vs right-handed pitchers

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

# Unknown-data neutral score ratio (used when trait data is missing)
UNKNOWN_SCORE_RATIO = 0.5             # default to mid-range when data unavailable

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
# 2 challenges per team per game).  So we apply a CONSERVATIVE adjustment:
# at most ±5% on the k_rate trait, scaled linearly by team framing runs.
#
# `framing_runs_floor` / `_ceiling`: the team-level framing_runs values
# that map to ±max adjustment.  A team at +12 runs/season gets +5% on k_rate;
# a team at -12 runs/season gets -5%.  Mid-pack teams (~0 runs) → no change.
SCORING_FRAMING_RUNS_CEILING = 12.0           # at or above → +max k_rate adjustment
SCORING_FRAMING_RUNS_FLOOR = -12.0            # at or below → -max k_rate adjustment
SCORING_FRAMING_K_RATE_MAX_ADJ = 0.05         # ±5% scale factor on k_rate
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
#   avg EV     ≥ AVG_EV_MAX (≈ 92 mph)       → 8 points
#   hard-hit % ≥ HARD_HIT_MAX (≈ 50%)        → 7 points
#   barrel %   ≥ BARREL_PCT_MAX              → 6 points
#   max EV     ≥ MAX_EV_CEILING (≈ 112 mph)  → 2 points
#   HR/PA      ≥ HR_PA_MAX                   → 2 points
# Sum is normalised by POWER_PROFILE_DENOM (25).
# ISO was removed as a power signal — it is a downstream SLG-AVG outcome that
# correlates with exit velocity but adds noise.  The V9.x code read ps.iso
# regardless and the MLB API never populated it.
POWER_PROFILE_BARREL_PCT_MAX = 15.0
POWER_PROFILE_AVG_EV_MAX = 92.0
POWER_PROFILE_HARD_HIT_MAX = 50.0
POWER_PROFILE_MAX_EV_CEILING = 112.0
POWER_PROFILE_DENOM = 25.0

# V10.8 — batter xwOBA scaling for power_profile.  Replaces some weight from
# HR/PA (a lagging outcome proxy that the MLB API never reliably populates)
# with the industry-standard quality-of-contact signal.  xwOBA derives from
# exit velocity + launch angle (and sprint speed for some batted balls); it
# captures the full value of contact, including weak grounders and warning-
# track flies that don't show up in HR counts.  See MLB Glossary on
# xwOBA: https://www.mlb.com/glossary/statcast/expected-woba
#
# Floor 0.300 = league-average wOBA (no credit), ceiling 0.400 = elite
# (Judge / Soto / Ohtani tier, full credit).  4 points out of the 25-pt
# power_profile denom — meaningful weight without dominating the kinematic
# signals.  Redistribution is balanced inside `score_power_profile` by
# trimming HR/PA from 2 → 1 and shaving 1 point off avg_ev / hard_hit
# (8+7→7+7); the denom stays at 25 so existing tests don't drift.
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

    # Slot multipliers must sum to a positive total
    assert sum(SLOT_MULTIPLIERS.values()) > 0, "SLOT_MULTIPLIERS must have positive values"
    assert SLOT_MULTIPLIERS[1] > SLOT_MULTIPLIERS[5], "Slot 1 must have the highest multiplier"

    # Park factors: must have at least one entry; COL should be the highest
    assert len(PARK_HR_FACTORS) >= 30, "PARK_HR_FACTORS missing teams"
    assert PARK_HR_FACTOR_MIN < 1.0 < PARK_HR_FACTOR_MAX, (
        f"PARK_HR_FACTOR range must straddle 1.0: [{PARK_HR_FACTOR_MIN}, {PARK_HR_FACTOR_MAX}]"
    )

    # DNP penalty tiers
    assert 0 < DNP_RISK_PENALTY < DNP_UNKNOWN_PENALTY < 1.0, (
        f"DNP penalties must satisfy 0 < RISK ({DNP_RISK_PENALTY}) < UNKNOWN ({DNP_UNKNOWN_PENALTY}) < 1"
    )

    # Stacking thresholds: shootout total must be higher than the PATH 1 total
    assert STACK_ELIGIBILITY_SHOOTOUT_TOTAL > STACK_ELIGIBILITY_VEGAS_TOTAL, (
        f"Shootout total ({STACK_ELIGIBILITY_SHOOTOUT_TOTAL}) must exceed PATH 1 total ({STACK_ELIGIBILITY_VEGAS_TOTAL})"
    )


_validate_constants()
