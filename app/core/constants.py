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
# V6.0 Popularity-First Signal Architecture
#
# The popularity signal is the DOMINANT ranking factor.  Empirical evidence
# across 20 dates: non-popular players hit HV at 63.6% vs 16.0% for popular
# ones.  The crowd-avoidance signal produces a 3.6x RS differential for
# batters (TARGET avg RS 3.57 vs FADE avg RS 0.98).
#
# The RS condition matrix (condition_classifier.py) provides calibrated
# factors, but these constants control the EV formula's structure:
#   pop_factor × env_factor × trait_factor × context × 100
#
# Popularity factor comes from the RS_CONDITION_MATRIX lookup.
# Env and trait factors are bounded to prevent them from overwhelming
# the popularity signal.
# ---------------------------------------------------------------------------

# Trait modifier bounds: trait scores are TIEBREAKERS, not drivers.
# Range: 0.75–1.25 (1.67x swing) — prevents a high-trait FADE from
# overtaking a low-trait TARGET.  The old 0.15–1.0 range (6.7x swing)
# let trait scores drown the popularity signal completely.
TRAIT_MODIFIER_FLOOR = 0.75
TRAIT_MODIFIER_CEILING = 1.25

# Env modifier bounds: second-strongest signal after popularity.
# Range: 0.60–1.40 (2.3x swing) — a great matchup can elevate a TARGET
# and a terrible matchup can sink one, but env can't rescue a FADE.
ENV_MODIFIER_FLOOR = 0.60
ENV_MODIFIER_CEILING = 1.40

# Legacy popularity constants — retained for backward compatibility.
# V6.0 uses the RS_CONDITION_MATRIX factors directly instead of these
# flat multipliers, but they remain for any code paths still referencing them.
POPULARITY_FADE_PENALTY = 0.75        # LEGACY: S5 FADE penalty
POPULARITY_TARGET_BONUS = 1.15        # LEGACY: S5 TARGET bonus

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
# Draft-count ownership leverage (§2 Pillar 1 + §9 Finding 1)
# Ghost ownership is THE #1 edge separating rank 1 from the field.
# 12/13 rank-1 lineups had at least 1 ghost player.
#
# These absolute thresholds are FALLBACKS only.  When the slate's draft
# distribution is available, ownership tiers are computed from empirical CDF
# percentiles (see condition_classifier.get_ownership_tier).  This makes the
# system slate-size-invariant: 100 drafts on a 2-game day is very different
# from 100 drafts on a 15-game day.
# ---------------------------------------------------------------------------
GHOST_DRAFT_THRESHOLD = 100           # FALLBACK: < 100 drafts = ghost player
LOW_DRAFT_THRESHOLD = 200             # FALLBACK: < 200 drafts = low-ownership differentiator
CHALK_DRAFT_THRESHOLD = 1500          # FALLBACK: >= 1500 drafts = chalk
MEGA_CHALK_DRAFT_THRESHOLD = 2000     # FALLBACK: >= 2000 drafts = mega-chalk

# Percentile-based ownership tier thresholds (empirical CDF)
# "Ghost" = bottom 15% of the draft distribution
# "Low" = 15th-35th percentile
# "Medium" = 35th-65th percentile
# "Chalk" = 65th-90th percentile
# "Mega-chalk" = top 10% AND requires minimum absolute draft count
OWNERSHIP_PERCENTILE_GHOST = 0.15     # bottom 15%
# Absolute draft-count floor for ghost classification.
# When the slate has a massive zero-draft pool (common: 30-40% of players
# have exactly 0 drafts), the 15th percentile can be 0, pushing players
# with 1-2 drafts out of the ghost tier.  This floor ensures micro-drafted
# players (the exact mega-ghosts we're hunting) are always classified ghost.
GHOST_ABSOLUTE_DRAFT_FLOOR = 25       # drafts <= 25 = always ghost, regardless of percentile
OWNERSHIP_PERCENTILE_LOW = 0.35       # 15th-35th
OWNERSHIP_PERCENTILE_MEDIUM = 0.65    # 35th-65th
OWNERSHIP_PERCENTILE_CHALK = 0.90     # 65th-90th
# Mega-chalk activation floor: even if a player is in the top 10% by
# percentile, they must also exceed this multiple of the median draft count
# to be classified mega-chalk.  Prevents false mega-chalk on thin slates.
MEGA_CHALK_MEDIAN_MULTIPLE = 3.0

# "Most drafted at 3x boost" trap — still flagged dynamically each run in the router.
# Historical bust rate: 57% with avg RS 0.72.
# Scales with slate size — floor of 3, ceiling of 7, proportional to
# the number of 3x-boost candidates on the slate.
MOST_DRAFTED_3X_TOP_N = 5             # default (overridden dynamically)
MOST_DRAFTED_3X_MIN_N = 3             # minimum (thin slates)
MOST_DRAFTED_3X_MAX_N = 7             # maximum (large slates)
MOST_DRAFTED_3X_PROPORTION = 0.30     # flag top 30% of 3x-boost pool

# Most-drafted-3x EV penalty.
# Players flagged as is_most_drafted_3x have a 57% bust rate, avg RS 0.72.
# Starting 5: env-aware — lighter when environmental support exists (crowd
# might know something), heavier when it doesn't (hype without support).
# Moonshot: always full penalty (max contrarian stance).
MOST_DRAFTED_3X_ENV_PASS_PENALTY = 0.80  # S5: 20% haircut when env >= ENV_PASS_THRESHOLD
MOST_DRAFTED_3X_PENALTY = 0.60            # S5: 40% haircut when env fails; Moonshot: always

# ---------------------------------------------------------------------------
# Ghost + Boost synergy constants
# Used for stack-building sort priority in _build_team_stack().
# The EV adjustments themselves are now in the condition matrix
# (app/services/condition_classifier.py).
# ---------------------------------------------------------------------------
GHOST_BOOST_SYNERGY_MIN_BOOST = 2.5   # minimum boost for ghost+boost stack priority
MEGA_GHOST_BOOST_MAX_DRAFTS = 50      # < 50 drafts + boost >= 3.0 = mega-ghost-boost tier (fallback ghost in validation)

# ---------------------------------------------------------------------------
# Lineup structure validation (§5 + §9 Finding 4)
# Every rank-1 lineup: 1 anchor + 2-3 differentiators + 1 flex
# ---------------------------------------------------------------------------
MAX_MEGA_CHALK_IN_LINEUP = 1          # max 1 player with 2000+ drafts
MIN_GHOST_IN_LINEUP = 1              # min 1 ghost player (< 100 drafts)
# Ghost enforcement: replace worst lineup player with a ghost if ghost EV >= this fraction
GHOST_ENFORCE_SWAP_THRESHOLD = 0.50  # was 0.70 — lowered so ghost inclusion actually fires
MAX_PLAYERS_PER_TEAM = 1             # 1 per team per individual lineup; correlation handled cross-lineup
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
# Soft auto-include, correlation, env tiebreaker
# ---------------------------------------------------------------------------

# Soft auto-include: ghost players with mid_boost (2.0-2.5) get priority over
# non-ghost candidates but after full auto-include (ghost + boost >= 2.5).
# Historical ghost+mid_boost HV rate = 0.75 — excellent, but not auto-tier 1.00.
# Captures players like James Wood (52 drafts, 2.0x, TV 16.8 on Apr 10).
SOFT_AUTO_INCLUDE_BOOST_THRESHOLD = 2.0  # ghost + boost >= 2.0 = soft auto-include

# Cross-lineup correlation: when a team has 2+ ghost players, they're likely
# in the same game environment.  Distributing one to Starting 5 and one to
# Moonshot captures correlated upside across both lineups.
CORRELATION_GHOST_MIN_PLAYERS = 2        # min ghost players on same team to trigger
CORRELATION_EV_BONUS = 1.10              # +10% EV for ghost players on correlation teams
CORRELATION_EV_BONUS_3PLUS = 1.15        # +15% EV when 3+ ghost teammates exist
MOONSHOT_CORRELATION_TEAMMATE_BONUS = 1.20  # +20% EV (replaces the -15% same-team penalty)

# Environmental tiebreaker for auto-include tier (condition_hv_rate >= 0.85).
# All ghost+max_boost look identical at condition_hv_rate=1.00.  This uses
# env_score to differentiate: a ghost+max player confirmed batting 3rd in Coors
# should rank above one with unknown batting order in Petco.
ENV_TIEBREAKER_BONUS_MAX = 0.15          # up to +15% EV based on env_score
ENV_TIEBREAKER_HV_THRESHOLD = 0.85       # only apply to high-HV-rate candidates

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
# Draft scarcity tiebreaker within auto-include tier
#
# April 11: All ghost+max_boost candidates had identical condition_hv_rate=1.00,
# making within-tier differentiation dependent on noisy rs_prob alone.
# The optimizer picked 9-15 draft players (García, Dingler, Ballesteros,
# Valenzuela) while winners were 1-4 draft players (Moniak, Laureano, Greene,
# Crawford, Bichette).
#
# Fewer drafts = deeper crowd asymmetry = higher edge.  A player with 1 draft
# is more "unknown" than one with 15 drafts — the crowd has priced in more
# information about the 15-draft player.
#
# Uses log scale for meaningful differentiation:
#   1 draft → +10% bonus, 5 drafts → +6.5%, 15 drafts → +4.1%, 50 → +1.5%
# ---------------------------------------------------------------------------
DRAFT_SCARCITY_TIEBREAKER_MAX = 0.10  # up to +10% EV bonus for ultra-low drafts
