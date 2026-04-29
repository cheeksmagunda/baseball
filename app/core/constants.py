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

# Draft evaluation: warn if user's lineup is this % worse than optimal
SUBOPTIMAL_THRESHOLD = 1.05  # 5% EV variance

# Minimum score threshold: players below this get a graduated EV penalty.
# Instead of a binary cliff (old: 50% haircut at <15), the penalty now scales
# linearly from MIN_SCORE_PENALTY_FLOOR at score=0 up to 1.0 at the threshold.
# This prevents ghost+boost players with score=14 from being treated identically
# to score=0 players.  See _graduated_score_penalty() in filter_strategy.py.
MIN_SCORE_THRESHOLD = 15  # out of 100 — full penalty below 0, no penalty at/above 15
MIN_SCORE_PENALTY_FLOOR = 0.40  # worst-case multiplier at score=0 (60% haircut)

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
DNP_UNKNOWN_PENALTY = 0.85            # UNKNOWN: 15% haircut (lineup not published, could go either way)
ENV_UNKNOWN_COUNT_THRESHOLD = 3       # >= this many unknown env factors = "data not published" (not "bad env")

# Env modifier bounds — PRIMARY EV signal.
# Range: 0.70–1.30 (1.86x swing) — game conditions (Vegas O/U, ERA, bullpen,
# park, weather, platoon, batting order, moneyline).
ENV_MODIFIER_FLOOR = 0.70
ENV_MODIFIER_CEILING = 1.30

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
# Moonshot constants (dual-lineup optimizer)
# ---------------------------------------------------------------------------

# Sharp signal bonus: underground analyst buzz (Reddit, FanGraphs, Prospects Live)
# → up to +35% EV.  Primary Moonshot differentiator from Starting 5.
MOONSHOT_SHARP_BONUS_MAX = 0.35

# Explosive bonus: power_profile (batters) or k_rate (pitchers) → up to +20% EV.
# Moonshot favors boom-or-bust profiles over balanced steady producers.
MOONSHOT_EXPLOSIVE_BONUS_MAX = 0.20

# V10.0: MOONSHOT_SAME_TEAM_PENALTY removed.  Artificially punishing stacks
# contradicts the correlation-driven strategy; Moonshot naturally diverges
# from Starting 5 via sharp_bonus × explosive_bonus re-ranking.

# ---------------------------------------------------------------------------
# Lineup structure validation
# ---------------------------------------------------------------------------
# MAX_PLAYERS_PER_TEAM replaced by MAX_PLAYERS_PER_TEAM_BATTERS above.
# Stacking up to 4 teammates (all batter slots) is explicitly allowed.
REQUIRED_PITCHERS_IN_LINEUP = 1      # exactly 1 pitcher per lineup
PITCHER_ANCHOR_SLOT = 1              # pitcher always in Slot 1 (2.0x)

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
# Graduated env-score scaling thresholds
#
# Every env factor uses the same pattern:
#   graduated_scale(value, floor, ceiling) → 0.0–1.0  (app.core.utils)
# These constants define the floor/ceiling for each factor so they are
# not scattered as magic numbers across filter_strategy.py.
# ---------------------------------------------------------------------------

# Pitcher env factors
PITCHER_ENV_OPS_CEILING = 0.780       # OPS at or above this → 0 contribution
PITCHER_ENV_OPS_FLOOR = 0.650         # OPS at or below this → full contribution
PITCHER_ENV_K_PCT_FLOOR = 0.20        # K% at or below this → 0 contribution
PITCHER_ENV_K_PCT_CEILING = 0.26      # K% at or above this → full contribution
PITCHER_ENV_K9_FLOOR = 6.0            # K/9 at or below this → 0 contribution
PITCHER_ENV_K9_CEILING = 10.0         # K/9 at or above this → full contribution
PITCHER_ENV_PARK_FLOOR = 0.90         # park factor at or below this → full contribution (pitcher-friendly)
PITCHER_ENV_PARK_CEILING = 1.05       # park factor at or above this → 0 contribution
PITCHER_ENV_ML_FLOOR = -130           # moneyline at or above this → 0 contribution
PITCHER_ENV_ML_CEILING = -220         # moneyline at or below this → full contribution
                                      # V10.2 (April 27 calibration): widened the
                                      # graduated band from (-110, -250) to (-130,
                                      # -220) after observing that ~25% of HV
                                      # pitchers across 33 historical slates pitched
                                      # for coin-flip or mild-favorite teams.  The
                                      # old floor zeroed-out ML credit for any
                                      # pitcher whose team wasn't already favored,
                                      # under-rating K-upside pitchers in tossup
                                      # games.  Aliased to BATTER_ENV_ML_* below.
PITCHER_ENV_MAX_SCORE = 5.5           # 5 main factors (1.0 each) + home (0.5)

# Batter env factors — Group A (run environment, soft-capped)
BATTER_ENV_VEGAS_FLOOR = 7.0          # O/U at or below this → 0 contribution
BATTER_ENV_VEGAS_CEILING = 9.5        # O/U at or above this → full contribution
BATTER_ENV_ERA_FLOOR = 3.5            # opposing starter ERA at or below → 0
BATTER_ENV_ERA_CEILING = 5.5          # opposing starter ERA at or above → full
# V10.4 (April 28 calibration): decoupled batter ML from pitcher ML.  The 33-slate
# game-level analysis shows mild favorites (-110 to -169) produce the MOST HV per
# game (1.27-1.32 HV/game vs 1.22 baseline), while strong favorites (-200 to -250)
# produce the LOWEST (1.14 HV/game).  The pre-V10.4 batter range was aliased to
# the pitcher range (-130 → -220), which gave full credit to the lowest-HV bucket
# and zero credit to the highest.  Reasoning: ML is a "team wins" signal which
# correlates with the OPPOSING starter being weak — but that's already scored
# directly via BATTER_ENV_ERA_*.  For batters, ML adds the most marginal signal
# in the mild-favorite zone where the game stays competitive (more PAs, deeper
# bullpen exposure, more late-inning leverage).  Centering the curve at -180
# captures this without over-rewarding extreme blowouts.
BATTER_ENV_ML_FLOOR = -100            # team_ml at or above (less negative) → 0 contribution
BATTER_ENV_ML_CEILING = -180          # team_ml at or below → full contribution (saturates)
BATTER_ENV_BULLPEN_ERA_FLOOR = 3.5    # bullpen ERA at or below → 0
BATTER_ENV_BULLPEN_ERA_CEILING = 5.5  # bullpen ERA at or above → full

# A5: Opposing starter WHIP (V10.3 calibration, Apr 27).  WHIP correlates with
# ERA at r=0.816 across 33 historical slates, but adds modest independent
# signal in the corners (low-ERA/high-WHIP starters get hit; high-ERA/low-WHIP
# starters stabilise).  Cross-tab on HV outcomes:
#     ERA <3.5, WHIP <1.20  → HV 38%
#     ERA <3.5, WHIP ≥1.40  → HV 50%
#     ERA ≥4.5, WHIP <1.20  → HV 37%
#     ERA ≥4.5, WHIP ≥1.40  → HV 53%
# Weight = 0.5 (half of ERA's 1.0 saturation contribution) reflects the smaller
# marginal HV swing while still letting Group A's soft cap absorb correlation.
BATTER_ENV_OPP_WHIP_FLOOR = 1.10      # opposing starter WHIP at or below → 0 (elite control)
BATTER_ENV_OPP_WHIP_CEILING = 1.40    # opposing starter WHIP at or above → full (vulnerable)
BATTER_ENV_OPP_WHIP_WEIGHT = 0.5      # max contribution to Group A run_env (half of ERA's 1.0)

# Group A soft cap: first 2.0 of correlated-signal sum is taken at full value,
# any additional sum above 2.0 contributes at 25% slope.  Preserves some upside
# for "perfect storm" games (all signals lit) without letting redundant signals
# multiply linearly.  V10.3: Group A has 5 signals — 4 main (O/U, ERA, ML,
# bullpen) at weight 1.0 + WHIP at weight 0.5 — so raw max is 4.5, soft-cap
# clamps it to 2.0 + 0.25×2.5 = 2.625 (was 2.5 pre-WHIP).  Note: WHIP scale
# (floor 1.10, ceiling 1.40) is separate from the scoring engine's WHIP scale
# (floor 0.9, ceiling 1.5 — `SCORING_BATTER_WHIP_*`); env scoring measures
# opponent vulnerability while scoring engine measures own-staff quality.
BATTER_ENV_GROUP_A_SOFT_CAP_POINT = 2.0
BATTER_ENV_GROUP_A_SOFT_CAP_SLOPE = 0.25

# Batter env factors — Group C (venue)
BATTER_ENV_PARK_HITTER_FRIENDLY = 1.05   # park factor at or above → full venue credit
BATTER_ENV_PARK_NEUTRAL = 1.0            # park factor at or above → partial credit
BATTER_ENV_WIND_SPEED_MIN = 10           # mph minimum for wind bonus
BATTER_ENV_WARM_TEMP_THRESHOLD = 80      # °F at or above → warm-weather bonus
BATTER_ENV_WARM_TEMP_BONUS = 0.2         # venue bonus for warm conditions
BATTER_ENV_WIND_OUT_BONUS = 0.5          # venue bonus for wind blowing out
BATTER_ENV_WIND_OUT_DIRECTIONS = ("OUT",)
# V10.3 (Apr 27 calibration): symmetrise wind direction.  Previously only OUT was
# scored, leaving wind blowing IN treated identical to neutral cross-wind.  HV
# rate analysis across 33 slates: wind OUT 52.9%, neutral cross-wind 48.0%, wind
# IN 45.8%.  IN suppresses HV by ~2.2pts (vs OUT's +4.9pts boost) — about half
# the magnitude — so the penalty is half of the OUT bonus.  Floor on `venue` at
# 0.0 matches the existing cold+pitcher-park compound penalty pattern.
BATTER_ENV_WIND_IN_PENALTY = 0.2         # venue penalty for wind blowing in
BATTER_ENV_WIND_IN_DIRECTIONS = ("IN",)

# Batter env factors — Group C compound (temp × park interaction)
BATTER_ENV_COMPOUND_HOT_THRESHOLD = 85      # °F above this triggers compound bonus
BATTER_ENV_COMPOUND_COLD_THRESHOLD = 55     # °F below this triggers compound penalty
BATTER_ENV_COMPOUND_PARK_THRESHOLD = 1.0    # park factor boundary (>1.0 = hitter, <1.0 = pitcher)
BATTER_ENV_COMPOUND_BONUS = 0.3             # additive to Group C for favorable correlated signals

# Batter env factors — Group D (series/momentum)
# Applied as bonus/deduction based on series context and recent form.
# A batter whose team trails 0-2 in a series and is on a cold L10 streak
# is in a genuinely bad situation regardless of their low media buzz.
SERIES_LEADING_BONUS = 0.6       # batter's team leads series 2-0 or better → +0.6
SERIES_TRAILING_PENALTY = 0.6    # batter's team trails series 0-2 or worse → -0.6
TEAM_HOT_L10_THRESHOLD = 7       # last-10 wins at or above → hot team bonus
TEAM_COLD_L10_THRESHOLD = 3      # last-10 wins at or below → cold team penalty
TEAM_HOT_L10_BONUS = 0.4         # bonus for hot team (last 10 ≥ 7 wins).
                                 # V10.2 (April 27 calibration): doubled from 0.2
                                 # to 0.4 after observing that hot-streak teams
                                 # consistently produced HV batters across the
                                 # 33-slate window (e.g., Apr 26 ATL 8-2 → 6 runs
                                 # at home).  Old 0.2 was a 3% env swing — below
                                 # the noise floor.
TEAM_COLD_L10_PENALTY = 0.4      # penalty for cold team (last 10 ≤ 3 wins).
                                 # V10.2: doubled from 0.2 to 0.4 (same rationale).

BATTER_ENV_MAX_SCORE = 6.0               # 2.0 (run env soft-cap point) + 2.0 (situation) + 1.0 (venue) + 1.0 (series/momentum).
                                         # V10.2: bumped from 5.8 to 6.0 because TEAM_HOT_L10_BONUS doubled (0.2 → 0.4),
                                         # so max momentum is now 1.0 (0.6 series leading + 0.4 hot L10) instead of 0.8.
                                         # Group A can reach 2.5 via soft slope in perfect-storm cases; the final
                                         # `min(1.0, total / max_score)` clamp preserves correct normalization.

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
POWER_PROFILE_HR_PA_MAX = 0.06
POWER_PROFILE_BARREL_PCT_MAX = 15.0
POWER_PROFILE_AVG_EV_MAX = 92.0
POWER_PROFILE_HARD_HIT_MAX = 50.0
POWER_PROFILE_MAX_EV_CEILING = 112.0
POWER_PROFILE_DENOM = 25.0

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

# Popularity classification thresholds (popularity.py::classify_player).
# Composite popularity score 0–100 (weighted blend of Google Trends, ESPN,
# search volume).  Player performance score is the 0–100 trait total from
# scoring_engine.
POPULARITY_HIGH_THRESHOLD = 50.0      # >= this = high media attention
POPULARITY_MID_THRESHOLD = 25.0       # [mid, high) = moderate buzz
POPULARITY_HIGH_PERF_THRESHOLD = 60.0 # >= this = strong performance signal
POPULARITY_MID_PERF_THRESHOLD = 25.0  # [mid, high) = decent performance

# V10.5 (April 28): bifurcate the FADE gate by position.
#
# Pre-V10.5, FADE was a hard exclusion for everyone.  Empirically this kept
# eliminating confirmed probable starters of heavy moneyline favorites
# (Ohtani, Yamamoto, Fried) — the crowd is correctly on these arms because
# pitcher outcomes are one-player-dependent and Vegas already prices them in.
# CLAUDE.md V8.0 strategy doc explicitly notes the pitcher TARGET-vs-FADE
# differential is 1.4× (vs the batter 3.0× swing).
#
# New rule:
#   - FADE batters → still excluded from the candidate pool (data shows the
#     crowd is ~3× wrong about batter ownership).
#   - FADE pitchers → kept in the pool, pay PITCHER_FADE_PENALTY in EV.
#     A genuinely strong pitcher (good env + good traits) can still beat a
#     FADE-untouched competitor; a weak pitcher cannot paper over the haircut.
#
# 0.85 = 15% haircut.  Inverse (1/0.85 ≈ 1.18) means a FADE pitcher needs
# ~18% more env+trait juice to displace a TARGET/NEUTRAL pitcher of equal
# raw ability — meaningful, not prohibitive.
PITCHER_FADE_PENALTY = 0.85


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

    # Pitcher env: floor must be less negative (higher) than ceiling
    # (e.g., -130 > -220 in numeric order)
    assert PITCHER_ENV_ML_FLOOR > PITCHER_ENV_ML_CEILING, (
        f"PITCHER_ENV_ML_FLOOR ({PITCHER_ENV_ML_FLOOR}) must be > CEILING ({PITCHER_ENV_ML_CEILING})"
    )
    assert PITCHER_ENV_OPS_FLOOR < PITCHER_ENV_OPS_CEILING, (
        f"PITCHER_ENV_OPS: floor ({PITCHER_ENV_OPS_FLOOR}) must be < ceiling ({PITCHER_ENV_OPS_CEILING})"
    )
    assert PITCHER_ENV_K9_FLOOR < PITCHER_ENV_K9_CEILING, (
        f"PITCHER_ENV_K9: floor ({PITCHER_ENV_K9_FLOOR}) must be < ceiling ({PITCHER_ENV_K9_CEILING})"
    )

    # Batter env: ML band — floor must be less negative than ceiling
    assert BATTER_ENV_ML_FLOOR > BATTER_ENV_ML_CEILING, (
        f"BATTER_ENV_ML_FLOOR ({BATTER_ENV_ML_FLOOR}) must be > CEILING ({BATTER_ENV_ML_CEILING})"
    )
    assert BATTER_ENV_VEGAS_FLOOR < BATTER_ENV_VEGAS_CEILING, (
        f"BATTER_ENV_VEGAS: floor ({BATTER_ENV_VEGAS_FLOOR}) must be < ceiling ({BATTER_ENV_VEGAS_CEILING})"
    )
    assert BATTER_ENV_ERA_FLOOR < BATTER_ENV_ERA_CEILING, (
        f"BATTER_ENV_ERA: floor ({BATTER_ENV_ERA_FLOOR}) must be < ceiling ({BATTER_ENV_ERA_CEILING})"
    )
    assert BATTER_ENV_OPP_WHIP_FLOOR < BATTER_ENV_OPP_WHIP_CEILING, (
        f"BATTER_ENV_OPP_WHIP: floor ({BATTER_ENV_OPP_WHIP_FLOOR}) must be < ceiling ({BATTER_ENV_OPP_WHIP_CEILING})"
    )

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
