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

# Minimum score threshold: players below this get an EV penalty
# Data shows ~65% of winning lineups have all players RS >= 1.0
# A low-scoring player with a huge boost is often a trap
MIN_SCORE_THRESHOLD = 15  # out of 100
MIN_SCORE_PENALTY = 0.50  # 50% EV haircut for below-threshold players

# ---------------------------------------------------------------------------
# Filter Strategy constants (§4 "Filter, Not Forecast")
# ---------------------------------------------------------------------------

# Slate classification thresholds (Filter 1)
TINY_SLATE_MAX_GAMES = 3
PITCHER_DAY_MIN_QUALITY_SP = 5   # 5+ quality SP matchups → pitcher day (strategy §4.2 Filter 1)
HITTER_DAY_MIN_HIGH_TOTAL = 5    # 5+ games with O/U >= 9.0 → hitter day
HITTER_DAY_VEGAS_TOTAL_THRESHOLD = 9.0

# Composition targets by slate type (pitcher count out of 5)
SLATE_COMPOSITION = {
    "tiny": {"min_pitchers": 1, "max_pitchers": 2},
    "pitcher_day": {"min_pitchers": 4, "max_pitchers": 5},
    "hitter_day": {"min_pitchers": 0, "max_pitchers": 1},
    "standard": {"min_pitchers": 2, "max_pitchers": 3},
}

# Boost-environment gating (Filter 4 — §4.2 Filter 4)
# A boost without environmental support is a trap (§3.5 "Boost Trap")
BOOST_NO_ENV_PENALTY = 0.70      # 30% EV haircut when boost has no env support
ENV_PASS_THRESHOLD = 0.5         # env_score must be > 0.5 (out of 1.0) to "pass"

# Game diversification (Filter 5 — §4.2 Filter 5 + Commandment 10)
MIN_GAMES_REPRESENTED = 2        # at least 2 different games in lineup
SAME_GAME_EXCESS_PENALTY = 0.80  # 20% penalty for 4th+ player from same game

# Environmental filter thresholds (Filter 2)
# Pitcher environmental pass conditions
PITCHER_ENV_WEAK_OPP_OPS = 0.700      # bottom-10 offense OPS threshold
PITCHER_ENV_WEAK_OPP_K_PCT = 0.24     # high-K% offense threshold
PITCHER_ENV_MIN_K_PER_9 = 8.0         # min K/9 for "K upside"
PITCHER_ENV_FRIENDLY_PARK = 1.00      # park factor below this = pitcher-friendly

# Batter environmental pass conditions
BATTER_ENV_HIGH_VEGAS_TOTAL = 8.5     # O/U >= this = high-run environment
BATTER_ENV_WEAK_PITCHER_ERA = 4.5     # opposing starter ERA above this = weak
BATTER_ENV_TOP_LINEUP = 4             # batting 1-4 = top of lineup

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
# Draft-count ownership leverage (§2.2, §4.2 Filter 3, Commandment 5)
# Based on actual draft counts, NOT web-scraped popularity.
# Ghost players with env support are the highest-EV pool historically.
# ---------------------------------------------------------------------------
GHOST_DRAFT_THRESHOLD = 100           # < 100 drafts = ghost player
GHOST_ENV_BONUS = 1.20                # 20% EV bonus for ghosts with env support
GHOST_MOONSHOT_ENV_BONUS = 1.30       # 30% EV bonus for ghosts in Moonshot
LOW_DRAFT_THRESHOLD = 200             # < 200 drafts = low-ownership differentiator
LOW_DRAFT_BONUS = 1.10                # 10% EV bonus
CHALK_DRAFT_THRESHOLD = 2000          # >= 2000 drafts = chalk (over-drafted)
CHALK_PENALTY = 0.85                  # 15% EV penalty for chalk
CHALK_EXEMPT_MIN_BOOST = 3.0          # Chalk exemption requires 3.0 boost + env pass

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
