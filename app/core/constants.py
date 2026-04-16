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
# Removed: MIN_SCORE_PENALTY = 0.50 — replaced by graduated scale

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

# SLATE_COMPOSITION removed in V2.1 — historical data (13 days) proves
# composition is driven purely by EV, not by "day type."
# Average winning lineup: 2.15 pitchers. Range: 0 to 5.
# Forcing min/max pitchers by slate type was the #1 source of bad lineups.
# The optimizer now uses pure EV ranking with no position constraints.

# ---------------------------------------------------------------------------
# Dynamic composition: boost-aware lineup building
# Historical data shows winning composition is driven by boost availability,
# not fixed position counts. From 4/2 onward, zero unboosted pitchers
# appeared in rank-1 lineups when quality boosted alternatives existed.
# ---------------------------------------------------------------------------
# A card is "quality boosted" if it has meaningful boost AND env support.
# boost >= 1.0 with env_score >= ENV_PASS_THRESHOLD.
BOOST_QUALITY_THRESHOLD = 1.0

# When this many quality boosted cards are available, let pure EV drive
# composition with no positional constraints.
BOOSTED_POOL_FULL_THRESHOLD = 5

ENV_PASS_THRESHOLD = 0.5           # env_score >= 0.5 = passes environmental filter

# Game diversification (Filter 5 — Law 9)
#
# Current rule (V5.0): max 1 player per game per lineup.  This is tighter than
# the earlier V3.1 cap of 3, which was intended to exploit team-stack data:
#   - Apr 6: Rank 1 = LAD+HOU stack (Ohtani, Freeman, Tucker, Hernandez, Rushing)
#   - Apr 5: OAK ghost stack dominated
#   - 62% of winning days featured team stacks of 3-4 players
# V3.2 tightened to 1 per team + 2 per game.  V3.3 dropped to 1 per game to
# capture stack upside cross-lineup (Starting 5 + Moonshot) instead of within
# a single lineup — see CORRELATION_* constants.
#
# MAX_OPPONENTS_SAME_GAME also caps 1: negative correlation (if one team's SP
# dominates, the other team's batters suffer) keeps opponents separated too.
MAX_PLAYERS_PER_GAME = 1         # max 1 player per game per lineup — full diversification
MAX_OPPONENTS_SAME_GAME = 1      # max 1 player from the opposing side of the same game
MIN_GAMES_REPRESENTED = 2        # at least 2 different games in lineup
SAME_GAME_EXCESS_PENALTY = 0.90  # 10% penalty for 4th+ player from same game

# ---------------------------------------------------------------------------
# Team stacking constants (§2 Pillar 2 — dominant on 62% of winning days)
# Within-lineup stacking is disabled (MAX_PLAYERS_PER_TEAM=1).  Correlation
# value is now captured cross-lineup via CORRELATION_* constants.  These
# constants are retained for _build_team_stack(), which is skipped when
# MAX_PLAYERS_PER_TEAM < STACK_MIN_PLAYERS.
# ---------------------------------------------------------------------------
STACK_MIN_PLAYERS = 3             # minimum players from same team to form a stack
STACK_MAX_PLAYERS = 4             # typical stack size (1-2 diversifiers from other games)
STACK_GHOST_BOOST_PRIORITY = True # prefer ghost-ownership players when stacking

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

# Debut/return premium (§2.3 Condition C)
DEBUT_RETURN_EV_BONUS = 1.15          # 15% EV bonus for debut/return players

# ---------------------------------------------------------------------------
# Bifurcated missing-data handling
#
# "Unknown environment" (missing data) ≠ "Bad environment" (confirmed bad).
# In DFS with convex payouts, uncertainty widens variance without shifting
# the mean.  A ghost player's missing batting order could be leadoff or DNP —
# penalizing as if it's DNP is asymmetrically wrong for high-boost players
# where any positive outcome crosses the threshold.
#
# Three tiers:
#   CONFIRMED_BAD: batting_order=None AND the player's team's lineup is
#                  published (so absence = genuinely not starting).
#   UNKNOWN:       batting_order=None AND lineup not yet published.
#                  Applies a lighter penalty reflecting true uncertainty.
#   GHOST_UNKNOWN: batting_order=None AND ghost-tier player.
#                  Lightest penalty — data scarcity is expected, not a signal.
# ---------------------------------------------------------------------------
DNP_RISK_PENALTY = 0.70               # CONFIRMED bad: 30% haircut (lineup published, player absent)
DNP_UNKNOWN_PENALTY = 0.85            # UNKNOWN: 15% haircut (lineup not published, could go either way)
DNP_GHOST_UNKNOWN_PENALTY = 0.92      # GHOST UNKNOWN: 8% haircut (data scarcity expected for ghosts)
ENV_UNKNOWN_COUNT_THRESHOLD = 3       # >= this many unknown env factors = "data not published" (not "bad env")

# ---------------------------------------------------------------------------
# V8.0 Pre-Game Signal Architecture
#
# Ownership counts and card boosts are only revealed during/after the draft
# and CANNOT be used as predictive inputs.  The EV formula is built entirely
# on signals that are knowable before any draft begins.
#
# Signal hierarchy (V8.0 — empirically calibrated):
#   1. pop_factor   — PRIMARY: media-attention crowd-avoidance signal from
#                    pre-game web sources (Google Trends, ESPN RSS, Reddit).
#                    3.0x swing.  Empirical basis: TARGET batters avg RS 3.57
#                    vs FADE batters avg RS 0.98 = 3.6x differential (20 dates).
#                    This is the sharpest pre-game predictor of RS — the crowd
#                    is structurally wrong about batters.  FADE aggressively.
#                    DFS platform ownership data EXCLUDED (during-draft only).
#   2. env_factor   — SECONDARY: game conditions available before first pitch
#                    (Vegas O/U, opposing starter ERA, park, weather, platoon,
#                     batting order, moneyline, bullpen ERA).  1.86x swing.
#   3. trait_factor — TERTIARY: season-level player quality (K/9, ISO,
#                    barrel%, SB pace, ERA, WHIP, recent form).  1.35x swing.
#
# Formula: base_ev = pop_factor × env_factor × trait_factor × context × 100
# ---------------------------------------------------------------------------

# Pop modifier bounds — PRIMARY signal (V8.0: elevated from tertiary).
# The RS_CONDITION_MATRIX raw factor (0.258–1.00) is scaled to this range.
# Range: 0.50–1.50 (3.0x swing) — matching the empirical 3.9x RS differential
# (TARGET batters avg RS 3.60 vs FADE batters avg RS 0.93, n=559, 21 dates).
# A FADE batter starts at 0.50x — needs 3x better env+trait to match TARGET.
# DFS platform ownership (RotoGrinders, NumberFire) is NOT included —
# it is only visible during the draft, not before.
POP_MODIFIER_FLOOR = 0.50
POP_MODIFIER_CEILING = 1.50
POP_FACTOR_RAW_MIN = 0.258   # min raw value from RS_CONDITION_MATRIX (batter FADE, V6.3)
POP_FACTOR_RAW_MAX = 1.00    # max raw value from RS_CONDITION_MATRIX (TARGET)

# Env modifier bounds — SECONDARY signal (V8.0: demoted from primary).
# Range: 0.70–1.30 (1.86x swing) — game environment differentiates within
# a popularity tier.  Cannot override a strong FADE/TARGET signal on its own.
ENV_MODIFIER_FLOOR = 0.70
ENV_MODIFIER_CEILING = 1.30

# Trait modifier bounds — TERTIARY signal (V8.0: demoted from secondary).
# Range: 0.85–1.15 (1.35x swing) — season stats provide fine-grained
# differentiation within the same env+pop tier.
TRAIT_MODIFIER_FLOOR = 0.85
TRAIT_MODIFIER_CEILING = 1.15

# ---------------------------------------------------------------------------
# Moonshot constants (dual-lineup optimizer)
# ---------------------------------------------------------------------------

# Moonshot popularity adjustments (heavier anti-crowd lean).
# V6.0: Moonshot uses RS_CONDITION_MATRIX factors with an additional
# contrarian multiplier that further penalizes FADE and rewards TARGET.
MOONSHOT_FADE_PENALTY = 0.60          # 40% penalty (vs 25% for Starting 5)
MOONSHOT_NEUTRAL_PENALTY = 0.95       # 5% penalty (if you're not a TARGET, step aside)
MOONSHOT_TARGET_BONUS = 1.30          # 30% bonus (vs 15% for Starting 5)
MOONSHOT_CONTRARIAN_FADE_MULT = 0.50  # V6.0: additional multiplier on matrix FADE factor
MOONSHOT_CONTRARIAN_TARGET_MULT = 1.25  # V6.0: additional multiplier on matrix TARGET factor

# Sharp signal bonus: underground buzz → up to +25% EV
MOONSHOT_SHARP_BONUS_MAX = 0.25

# Explosive bonus: power_profile (batters) or k_rate (pitchers) → up to +10% EV
MOONSHOT_EXPLOSIVE_BONUS_MAX = 0.10

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
# V6.0: retains V5.0 composition — exactly 1 pitcher + 4 batters.
# The pitcher anchors Slot 1 (2.0x multiplier).  The popularity-first EV
# formula determines WHICH pitcher and WHICH 4 batters, but the 1P+4B
# shape is fixed.
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
# Boost concentration penalty (§4.2 Filter 4)
# Don't put all boosted players in the same game.
# ---------------------------------------------------------------------------
BOOST_CONCENTRATION_THRESHOLD = 3     # 3+ boosted in same game triggers penalty
BOOST_CONCENTRATION_PENALTY = 0.85    # 15% penalty for 3rd+ boosted in same game

# ---------------------------------------------------------------------------
# V5.0: Slot 1 Differentiator Principle RETIRED.
# Slot 1 is permanently reserved for the anchor pitcher (see PITCHER_ANCHOR_SLOT).
# The contrarian-swap heuristic no longer applies.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# V4.1: Rich-pool unboosted pitcher penalty REMOVED.
# The recalibrated condition matrix (V4.0) encodes empirical HV rates per
# (ownership × boost) cell.  Elite unboosted aces (Sale/Alcantara/Fried class)
# now rate 0.19–0.43 on the pitcher matrix — the matrix already accounts for
# their unboosted-ness.  Stacking a 10–35% haircut on top double-counted and
# buried the anchor plays those aces provide.  Historical counter-examples
# (Nolan McLean Apr 9, Sandy Alcantara Apr 7, Max Fried recurring) all surface
# correctly through the recalibrated matrix alone.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# V5.0: Dynamic pitcher cap RETIRED.
#
# The V3.0-V3.4 dynamic pitcher cap (1/2/3 based on boosted-pool richness) is
# replaced by a hard 1-pitcher anchor rule.  See REQUIRED_PITCHERS_IN_LINEUP
# and PITCHER_ANCHOR_SLOT above.  Deprecated constants removed:
#   - MAX_PITCHERS_THIN_POOL
#   - MAX_PITCHERS_BOOSTED_RICH
#   - BOOSTED_PITCHER_CAP_EXPAND_MIN
#   - PITCHER_CAP_EV_THRESHOLD
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pitcher-specific FADE moderation
#
# Pitchers control their own environment — high draft count reflects real
# ERA/K-rate performance data, not media hype.  The crowd is structurally
# LESS wrong about pitchers than batters because pitcher outcomes are more
# predictable (one player controls the game vs. batters needing team context).
#
# Historical evidence:
#   - Apr 11: Suarez (2.2k drafts, RS 5.7), Sheehan (1.9k, RS 2.8),
#     Bassitt (1.5k, RS 2.3) — all in 5/6 top lineups despite being FADE
#   - Apr 7: Eovaldi (in 11/12 top lineups despite high ownership)
#   - PITCHER_CONDITION_MATRIX chalk+max_boost = 0.50 HV rate (5x batter rate)
#
# Starting 5: 15% haircut (vs 25% for batters)
# Moonshot: 30% haircut (vs 40% for batters)
# ---------------------------------------------------------------------------
PITCHER_FADE_PENALTY = 0.85           # S5: 15% haircut (batters: 0.75 = 25%)
MOONSHOT_PITCHER_FADE_PENALTY = 0.70  # Moonshot: 30% haircut (batters: 0.60 = 40%)

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
PITCHER_ENV_MAX_SCORE = 6.0           # 5 main factors (1.0) + home (0.5) + debut (0.5)

# Batter env factors — Group A (run environment, capped at 2.0)
BATTER_ENV_VEGAS_FLOOR = 7.0          # O/U at or below this → 0 contribution
BATTER_ENV_VEGAS_CEILING = 9.5        # O/U at or above this → full contribution
BATTER_ENV_ERA_FLOOR = 3.5            # opposing starter ERA at or below → 0
BATTER_ENV_ERA_CEILING = 5.5          # opposing starter ERA at or above → full
BATTER_ENV_ML_FLOOR = -110            # same as pitcher (shared graduation)
BATTER_ENV_ML_CEILING = -250          # same as pitcher (shared graduation)
BATTER_ENV_BULLPEN_ERA_FLOOR = 3.5    # bullpen ERA at or below → 0
BATTER_ENV_BULLPEN_ERA_CEILING = 5.5  # bullpen ERA at or above → full

# Batter env factors — Group C (venue)
BATTER_ENV_PARK_HITTER_FRIENDLY = 1.05   # park factor at or above → full venue credit
BATTER_ENV_PARK_NEUTRAL = 1.0            # park factor at or above → partial credit
BATTER_ENV_WIND_SPEED_MIN = 10           # mph minimum for wind bonus
BATTER_ENV_WARM_TEMP_THRESHOLD = 80      # °F at or above → warm-weather bonus
BATTER_ENV_WARM_TEMP_BONUS = 0.2         # venue bonus for warm conditions
BATTER_ENV_WIND_OUT_BONUS = 0.5          # venue bonus for wind blowing out
BATTER_ENV_WIND_OUT_DIRECTIONS = ("OUT", "L TO R", "R TO L", "OUT TO CF")

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

# Raised from 5.5 to 6.3 to accommodate Group D headroom (max 0.8 additive).
BATTER_ENV_MAX_SCORE = 6.3               # 2.0 (run env cap) + 2.0 (situation) + 1.0 (venue) + 0.5 (debut) + 0.8 (series/momentum)

# Momentum gate — caps pop_factor at NEUTRAL for batters simultaneously trailing
# in series AND on a cold L10 streak.  Prevents TARGET misclassification of
# "correctly-avoided" players (e.g., Red Sox batter in a sweep).
MOMENTUM_GATE_SERIES_DEFICIT = 2   # series deficit at or above triggers gate
MOMENTUM_GATE_L10_CEILING = 3      # L10 wins at or below triggers gate (cold + trailing)

# ---------------------------------------------------------------------------
# Game status constants
# Games in these statuses will never receive scores; treat as "done" so the
# post-lock monitor and cache completion check don't perma-freeze.
# ---------------------------------------------------------------------------
NON_PLAYING_GAME_STATUSES = frozenset({"Postponed", "Cancelled", "Suspended"})

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

