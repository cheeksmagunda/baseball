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
# V7.0 Pre-Game Signal Architecture
#
# Ownership counts and card boosts are only revealed during/after the draft
# and CANNOT be used as predictive inputs.  The EV formula is built entirely
# on signals that are knowable before any draft begins.
#
# Signal hierarchy:
#   1. env_factor   — PRIMARY: game conditions available before first pitch
#                    (Vegas O/U, opposing starter ERA, park, weather, platoon,
#                     batting order, moneyline, bullpen ERA).  3.0x swing.
#   2. trait_factor — SECONDARY: season-level player quality (K/9, ISO,
#                    barrel%, SB pace, ERA, WHIP, recent form).  1.86x swing.
#   3. pop_factor   — TERTIARY: media attention from pre-game web signals
#                    (Google Trends, ESPN RSS, Reddit).  1.35x swing.
#                    DFS platform ownership data EXCLUDED (during-draft only).
#
# Formula: base_ev = env_factor × trait_factor × pop_factor × context × 100
# ---------------------------------------------------------------------------

# Trait modifier bounds — SECONDARY signal.
# Range: 0.70–1.30 (1.86x swing) — season stats differentiate within an
# environment tier.  Cannot override a great or terrible matchup on its own.
TRAIT_MODIFIER_FLOOR = 0.70
TRAIT_MODIFIER_CEILING = 1.30

# Env modifier bounds — PRIMARY signal (expanded from V6.0 0.60–1.40).
# Range: 0.50–1.50 (3.0x swing) — the game environment is the strongest
# pre-game predictor: Vegas O/U, opposing ERA, park, weather, batting order.
ENV_MODIFIER_FLOOR = 0.50
ENV_MODIFIER_CEILING = 1.50

# Pop modifier bounds — TERTIARY signal.
# The RS_CONDITION_MATRIX raw factor (0.275–1.00) is compressed into this
# narrower range so crowd-avoidance context modulates rather than dominates.
# Range: 0.85–1.15 (1.35x swing).
# DFS platform ownership (RotoGrinders, NumberFire) is NOT included —
# it is only visible during the draft, not before.
POP_MODIFIER_FLOOR = 0.85
POP_MODIFIER_CEILING = 1.15
POP_FACTOR_RAW_MIN = 0.275   # min raw value from RS_CONDITION_MATRIX (batter FADE)
POP_FACTOR_RAW_MAX = 1.00    # max raw value from RS_CONDITION_MATRIX (TARGET)

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

