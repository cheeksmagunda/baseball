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
    "ATH": 0.90,  # Sacramento (Sutter Health Park) — using neutral estimate, no 2026 data yet
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

ENV_PASS_THRESHOLD = 0.5           # env_score >= 0.5 = passes environmental filter

MAX_PLAYERS_PER_GAME = 1         # max 1 player per game per lineup — full diversification
MAX_OPPONENTS_SAME_GAME = 1      # max 1 player from the opposing side of the same game
MIN_GAMES_REPRESENTED = 2        # at least 2 different games in lineup
SAME_GAME_EXCESS_PENALTY = 0.90  # 10% penalty for 4th+ player from same game

# Environmental filter thresholds (Filter 2)
# Pitcher environmental pass conditions
PITCHER_ENV_WEAK_OPP_OPS = 0.700      # bottom-10 offense OPS threshold
PITCHER_ENV_WEAK_OPP_K_PCT = 0.24     # high-K% offense threshold
PITCHER_ENV_MIN_K_PER_9 = 8.0         # min K/9 for "K upside"
PITCHER_ENV_FRIENDLY_PARK = 1.00      # park factor below this = pitcher-friendly

# Batter environmental pass conditions
BATTER_ENV_HIGH_VEGAS_TOTAL = 8.5     # O/U >= this = high-run environment
BATTER_ENV_WEAK_PITCHER_ERA = 4.5     # opposing starter ERA above this = weak
BATTER_ENV_TOP_LINEUP = 5             # batting 1-5 = top of lineup (§4)
BATTER_ENV_WEAK_BULLPEN_ERA = 4.5     # opposing bullpen ERA above this = vulnerable

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

# Game diversification: soft penalty for same-team overlap with Starting 5
MOONSHOT_SAME_TEAM_PENALTY = 0.85

# ---------------------------------------------------------------------------
# Draft-count reference values (INFORMATIONAL ONLY — not EV inputs)
#
# Ownership counts and card boosts are only revealed during/after the draft
# and are NOT used as predictive inputs in the EV formula.
# These constants are retained solely for logging/display purposes so the
# response payload can label players by draft tier for the user's context.
# ---------------------------------------------------------------------------
GHOST_DRAFT_THRESHOLD = 100           # < 100 drafts = ghost (display label only)
CHALK_DRAFT_THRESHOLD = 1500          # >= 1500 drafts = chalk (display label only)
MEGA_CHALK_DRAFT_THRESHOLD = 2000     # >= 2000 drafts = mega-chalk (display label only)

# ---------------------------------------------------------------------------
# Lineup structure validation
# ---------------------------------------------------------------------------
MAX_PLAYERS_PER_TEAM = 1             # 1 per team per individual lineup
REQUIRED_PITCHERS_IN_LINEUP = 1      # exactly 1 pitcher per lineup
MAX_PITCHERS_IN_LINEUP = 1           # identical to REQUIRED
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
PITCHER_ENV_ML_FLOOR = -110           # moneyline at or above this → 0 contribution
PITCHER_ENV_ML_CEILING = -250         # moneyline at or below this → full contribution
PITCHER_ENV_MAX_SCORE = 5.5           # 5 main factors (1.0 each) + home (0.5)

# Batter env factors — Group A (run environment, soft-capped)
BATTER_ENV_VEGAS_FLOOR = 7.0          # O/U at or below this → 0 contribution
BATTER_ENV_VEGAS_CEILING = 9.5        # O/U at or above this → full contribution
BATTER_ENV_ERA_FLOOR = 3.5            # opposing starter ERA at or below → 0
BATTER_ENV_ERA_CEILING = 5.5          # opposing starter ERA at or above → full
BATTER_ENV_ML_FLOOR = PITCHER_ENV_ML_FLOOR    # moneyline graduation shared with pitcher env
BATTER_ENV_ML_CEILING = PITCHER_ENV_ML_CEILING
BATTER_ENV_BULLPEN_ERA_FLOOR = 3.5    # bullpen ERA at or below → 0
BATTER_ENV_BULLPEN_ERA_CEILING = 5.5  # bullpen ERA at or above → full

# Group A soft cap: first 2.0 of correlated-signal sum is taken at full value,
# any additional sum above 2.0 contributes at 25% slope.  Preserves some upside
# for "perfect storm" games (all 4 signals lit) without letting redundant
# signals multiply linearly.  Raw max under 4 perfect signals: 2.0 + 0.25×2.0 = 2.5.
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
TEAM_HOT_L10_BONUS = 0.2         # bonus for hot team (last 10 ≥ 7 wins)
TEAM_COLD_L10_PENALTY = 0.2      # penalty for cold team (last 10 ≤ 3 wins)

BATTER_ENV_MAX_SCORE = 5.8               # 2.0 (run env soft-cap point) + 2.0 (situation) + 1.0 (venue) + 0.8 (series/momentum).
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
# HR/PA ≥ HR_PA_MAX → 10 points; barrel% ≥ BARREL_PCT_MAX → 8 points;
# ISO ≥ ISO_MAX → 7 points.  Sum is normalised by POWER_PROFILE_DENOM (25).
POWER_PROFILE_HR_PA_MAX = 0.06
POWER_PROFILE_BARREL_PCT_MAX = 15.0
POWER_PROFILE_ISO_MAX = 0.250
POWER_PROFILE_DENOM = 25.0

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


