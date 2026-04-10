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
    "OAK": 0.90,  # Coliseum
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
# V2: Pitcher Day = 23% of slates (not the default), Hitter/Stack Day = 38%
TINY_SLATE_MAX_GAMES = 3
PITCHER_DAY_MIN_QUALITY_SP = 4   # 4+ quality SP matchups → pitcher day (V2 §3)
HITTER_DAY_MIN_HIGH_TOTAL = 4    # 4+ games with O/U >= 9.0 → hitter day (V2 §3)
HITTER_DAY_VEGAS_TOTAL_THRESHOLD = 9.0

# Blowout detection (V2 §2 Pillar 2 + §3 checklist)
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

# Game diversification (Filter 5 — V2 Law 9)
MIN_GAMES_REPRESENTED = 2        # at least 2 different games in lineup
SAME_GAME_EXCESS_PENALTY = 0.90  # 10% penalty for 5th player from same game (softened for stacking)

# ---------------------------------------------------------------------------
# Team stacking constants (V2 §2 Pillar 2 — dominant on 62% of winning days)
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
BATTER_ENV_TOP_LINEUP = 5             # batting 1-5 = top of lineup (V2 §4)

# Debut/return premium (§2.3 Condition C)
DEBUT_RETURN_EV_BONUS = 1.15          # 15% EV bonus for debut/return players

# ---------------------------------------------------------------------------
# Popularity-based EV adjustments (web-scraped FADE/TARGET/NEUTRAL)
# ---------------------------------------------------------------------------

# Starting 5: standard adjustments (same as draft_optimizer)
POPULARITY_FADE_PENALTY = 0.75        # 25% EV penalty — crowd is already here
POPULARITY_TARGET_BONUS = 1.15        # 15% EV bonus — under the radar edge

# ---------------------------------------------------------------------------
# Moonshot constants (dual-lineup optimizer)
# ---------------------------------------------------------------------------

# Moonshot popularity adjustments (heavier anti-crowd lean)
MOONSHOT_FADE_PENALTY = 0.60          # 40% penalty (vs 25% for Starting 5)
MOONSHOT_NEUTRAL_PENALTY = 0.95       # 5% penalty (if you're not a TARGET, step aside)
MOONSHOT_TARGET_BONUS = 1.30          # 30% bonus (vs 15% for Starting 5)

# Sharp signal bonus: underground buzz → up to +25% EV
MOONSHOT_SHARP_BONUS_MAX = 0.25

# Explosive bonus: power_profile (batters) or k_rate (pitchers) → up to +10% EV
MOONSHOT_EXPLOSIVE_BONUS_MAX = 0.10

# Game diversification: soft penalty for same-team overlap with Starting 5
MOONSHOT_SAME_TEAM_PENALTY = 0.85

# ---------------------------------------------------------------------------
# Draft-count ownership leverage (V2 §2 Pillar 1 + §9 Finding 1)
# Ghost ownership is THE #1 edge separating rank 1 from the field.
# 12/13 rank-1 lineups had at least 1 ghost player.
# ---------------------------------------------------------------------------
GHOST_DRAFT_THRESHOLD = 100           # < 100 drafts = ghost player
LOW_DRAFT_THRESHOLD = 200             # < 200 drafts = low-ownership differentiator
CHALK_DRAFT_THRESHOLD = 1500          # >= 1500 drafts = chalk
MEGA_CHALK_DRAFT_THRESHOLD = 2000     # >= 2000 drafts = mega-chalk

# "Most drafted at 3x boost" trap — still flagged dynamically each run in the router.
# Historical bust rate: 57% with avg RS 0.72.
# How many of the most-drafted 3x-boost players to flag per slate.
MOST_DRAFTED_3X_TOP_N = 5

# ---------------------------------------------------------------------------
# Ghost + Boost synergy constants
# Used for stack-building sort priority in _build_team_stack().
# The EV adjustments themselves are now in the condition matrix
# (app/services/condition_classifier.py).
# ---------------------------------------------------------------------------
GHOST_BOOST_SYNERGY_MIN_BOOST = 2.5   # minimum boost for ghost+boost stack priority
MEGA_GHOST_BOOST_MAX_DRAFTS = 50      # < 50 drafts + boost >= 3.0 = mega-ghost-boost tier (fallback ghost in validation)

# ---------------------------------------------------------------------------
# Lineup structure validation (V2 §5 + §9 Finding 4)
# Every rank-1 lineup: 1 anchor + 2-3 differentiators + 1 flex
# ---------------------------------------------------------------------------
MAX_MEGA_CHALK_IN_LINEUP = 1          # max 1 player with 2000+ drafts
MIN_GHOST_IN_LINEUP = 1              # min 1 ghost player (< 100 drafts)
# Ghost enforcement: replace worst lineup player with a ghost if ghost EV >= this fraction
GHOST_ENFORCE_SWAP_THRESHOLD = 0.50  # was 0.70 — lowered so ghost inclusion actually fires
MAX_PLAYERS_PER_TEAM = 1             # max 1 player from any single team per lineup

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
# Slot 1 Differentiator Principle (§4.2 Filter 5, §3.4)
# When the field converges on an obvious Slot 1, put the contrarian there.
# ---------------------------------------------------------------------------
SLOT1_DIFFERENTIATOR_EV_THRESHOLD = 0.90  # Only swap if contrarian within 10% EV

# ---------------------------------------------------------------------------
# Rich-pool pitcher correction (V2 §4.3 dynamic composition rule)
# When the boosted pool is full (≥ BOOSTED_POOL_FULL_THRESHOLD quality cards),
# unboosted pitchers get de-prioritized. Historical data (4/2 onward): zero
# unboosted pitchers appeared in rank-1 lineups when quality boosted
# alternatives existed. Boost amplifies RS — an unboosted pitcher at RS 5.5
# still loses to a ghost-boosted batter with RS 2.5 + boost 3.0.
# ---------------------------------------------------------------------------
UNBOOSTED_PITCHER_RICH_POOL_PENALTY = 0.65  # 35% EV haircut
