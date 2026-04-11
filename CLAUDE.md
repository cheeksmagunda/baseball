# Baseball DFS Predictor - AI Assistant Guide

## CRITICAL: This Is NOT Traditional DFS

This is **Real Sports DFS**. There is no salary cap. Players are drafted into 5 fixed slots with multipliers (2.0, 1.8, 1.6, 1.4, 1.2). Each player card has a **card_boost** (0 to +3.0x).

### The Core Formula (single source of truth: `app/core/utils.py`)

```
total_value = real_score × (2 + card_boost)
```

- `BASE_MULTIPLIER = 2.0` is defined in `app/core/utils.py`
- `compute_total_value()` is the ONLY place this formula should be computed
- Slot multipliers are separate and defined in `app/core/constants.py`
- Lineup score = Σ real_score × (slot_mult + card_boost)

**Never hardcode `(2 + card_boost)` or `(2.0 + card_boost)` anywhere.** Always import from `app/core/utils.py`.

## ABSOLUTE RULE: No Fallbacks. Ever.

**Never add fallback behavior to the pipeline.** If today's data isn't available, return an error. Do not:
- Fall back to the most recent slate
- Substitute probable pitchers when a boxscore returns no players
- Use seed/historical data as a substitute for live data
- Return stale cached results when fresh data fails

The pipeline either works with real data or it fails loudly. Fallbacks mask bugs, corrupt optimization with wrong data, and violate the "Filter, Not Forecast" philosophy — you cannot filter on yesterday's environment.

## Architecture Overview

### Active Pipeline

The active optimization path is `filter_strategy` — **not** `draft_optimizer.py` (which is dead code kept only for `evaluate_lineup`).

**Four-Stage Pipeline:**
1. **Collect** (`app/services/data_collection.py`) — Fetch MLB schedule + boxscores + player stats
2. **Score** (`app/services/scoring_engine.py`) — Rate players 0-100 via trait profiles
3. **Filter** (`app/services/filter_strategy.py`) — Apply five sequential filters (§4 strategy)
4. **Optimize** (`app/routers/filter_strategy.py` → `run_dual_filter_strategy`) — Produce Starting 5 + Moonshot

### Philosophy
Rule-based scoring + external-variables filtering (NOT ML). The goal is to **win drafts**, not predict Real Score. RS is opaque — we estimate via player profiling and filter on pre-game conditions.

## Data Files (`/data/`)

| File | Purpose |
|---|---|
| `historical_players.csv` | Master player ledger (1 row per player/day). Contains total_value, card_boost, drafts, leaderboard flags |
| `historical_winning_drafts.csv` | Top 20 lineups (5 rows per lineup) |
| `historical_slate_results.json` | MLB game outcomes by date |
| `hv_player_game_stats.csv` | Actual box score stats for 98 Highest Value player appearances |

## Database Models (9 tables)

| Model | Table | Key Fields |
|---|---|---|
| `Player` | players | name, name_normalized, team, position, mlb_id |
| `PlayerStats` | player_stats | Season aggregates (batting + pitching) |
| `PlayerGameLog` | player_game_log | Per-game records (H, HR, RBI, IP, K, ER, etc.) |
| `Slate` | slates | date, game_count, status |
| `SlateGame` | slate_games | home_team, away_team, scores, Vegas lines, starter ERA/K9, weather |
| `SlatePlayer` | slate_players | card_boost, real_score, total_value, batting_order, env_score, leaderboard flags |
| `PlayerScore` | player_scores | total_score (0-100), estimated RS range |
| `ScoreBreakdown` | score_breakdowns | Per-trait scores |
| `DraftLineup` | draft_lineups | source, expected/actual values |
| `DraftSlot` | draft_slots | slot_index, slot_mult, card_boost |
| `WeightHistory` | weight_history | weights_json, effective_date |

## Scoring Engine (`app/services/scoring_engine.py`)

**Pitcher traits** (5 traits, 0-100): ace_status(25), k_rate(25), matchup_quality(20), recent_form(15), era_whip(15)

**Batter traits** (7 traits, 0-100): power_profile(25), matchup_quality(20), lineup_position(15), recent_form(15), ballpark_factor(10), hot_streak(10), speed_component(5)

Weights are configurable via the weights API (`GET/PUT /api/calibration/weights`).

## Shared Utilities (`app/core/utils.py`)

All shared formulas and lookups live here. **Always use these instead of reimplementing:**

- `compute_total_value(real_score, card_boost)` — The core formula
- `find_player_by_name(db, name, team)` — Accent-insensitive player lookup
- `get_latest_player_score(db, slate_player_id)` — Most recent PlayerScore
- `get_recent_games(game_logs, n)` — N most recent games sorted by date
- `scale_score(value, floor, ceiling, max_pts)` — Linear scaling helper

## Popularity Signal Aggregator (`app/services/popularity.py`)

Web-scraping signal aggregator that estimates which players the crowd will over-draft. This is NOT rule-based — it's dynamic, fetching real-time external signals.

**Signal sources (weighted):**
- Social trending (40%): Google Trends autocomplete + daily trends
- Sports news (20%): ESPN and MLB.com RSS feeds
- DFS ownership (20%): RotoGrinders, NumberFire cross-platform ownership
- Search volume (20%): Google autocomplete context terms

**Classification logic:**
- High attention + any performance level → **FADE** (crowd is already here)
- High performance + low attention → **TARGET** (under the radar)
- Low attention + mid performance → **TARGET** (value pick)
- Otherwise → **NEUTRAL**

**Optimizer integration:** FADE players get 25% EV penalty, TARGET players get 15% EV bonus. Constants in `app/core/constants.py`. Boost math still dominates — a FADE with +3.0x boost still beats a TARGET with no boost.

**Key distinction:** "Trending" ≠ "popular." A breakout rookie trending upward (TARGET) is different from a slumping star trending on ESPN (FADE). The aggregator distinguishes by cross-referencing attention volume against performance score.

**Sharp signal (underground):** A 5th source scraped from Reddit (r/fantasybaseball, r/baseball), FanGraphs community blogs, and Prospects Live. Used exclusively by the Moonshot lineup. If niche smart accounts are on a player but mainstream isn't, that's a Moonshot BUY. `sharp_score` is 0-100, separate from the composite score.

## Dual-Lineup Optimizer (`app/services/filter_strategy.py`)

**Strategy Version: V3.3 "Correlate, Differentiate, Distribute"** — V3.3: cross-lineup correlation, three-tier composition, env tiebreaker, MAX_PLAYERS_PER_TEAM=1, MAX_PLAYERS_PER_GAME=1.

The active optimizer produces **two lineups** from the same candidate pool via `run_dual_filter_strategy`.

### The Primary Signal: Draft Tier × Boost (Proven from 15 dates)

The optimizer's job is NOT to predict RS. It is to identify which players are in the **ghost+high-boost category** — that category's historical win rate is itself the signal:

| Draft tier | n | Avg TV | % TV>15 |
|---|---|---|---|
| mega-ghost (<50 drafts) + boost ≥ 2.0 | 119 | 19.9 | **82%** |
| ghost (50–99 drafts) + boost ≥ 2.0 | 4 | 20.7 | **100%** |
| medium (200–499 drafts) + boost ≥ 2.0 | 9 | 2.5 | 0% |
| mid-chalk (500–1499 drafts) + boost ≥ 2.0 | 34 | 3.1 | 12% |

Ghost+boost players have 82–100% historical TV>15 rate. Medium/chalk+boost players have 0–12%. **The trait score matters far less than which tier the player sits in.**

### Three Pillars (V2)

1. **Ghost Ownership (#1 edge):** 12/13 rank-1 lineups had ≥1 ghost player (<100 drafts). Mega-ghost+boost (<50 drafts + boost ≥ 3.0) gets auto-include-level EV bonus — env gate removed (see V2.4).
2. **Team Stacking:** Dominant winning pattern on 62% of days. On hitter/stack days, stack 3-4 from the favored team's ghost pool + 1-2 diversifiers.
3. **Boost Leverage:** "Most drafted at 3x boost" busts 57% of the time — it's a SELL signal. The top-5 most-drafted 3x players are dynamically penalized each run. Ghost+boost is the buy signal.

### EV Formula (V3.2 — 4-term condition-based, single source: `_compute_base_ev()`)
```
filter_ev = condition_hv_rate                    # Term 1: from CONDITION_MATRIX (primary signal)
  × rs_prob                                       # Term 2: P(RS >= 15/(2+boost)) from traits
  × stack_bonus (1.20 if blowout game, else 1.0)  # Term 3: blowout team bonus
  × anti_crowd (FADE=0.75, TARGET=1.15)            # Term 4: popularity adjustment
  × debut_bonus (1.15 if first appearance)
  × dnp_adj (ghost=0.92, unknown=0.85, confirmed_bad=0.70)
  × env_tiebreaker (up to +15% for HV rate ≥ 0.85) # V3.2: differentiates among auto-includes
  × correlation_bonus (1.10-1.15 for correlation teams) # V3.2: cross-lineup correlation
  × 100
```
Note: card_boost is NOT multiplied again — it's already captured in condition_hv_rate (ghost+3.0x → HV rate 1.00) and rs_prob (higher boost → lower RS threshold).

Post-EV adjustments (applied in `run_filter_strategy`):
- Unboosted pitcher penalty (`_apply_unboosted_pitcher_penalty`): 10-35% haircut when boosted pool is rich
- Three-tier ordering: auto-include → soft_auto_include → rest by filter_ev

### V2.4 Changes (April 9 — April 8 Post-Mortem)

**April 8 results:** Our draft had zero true ghost players. All five optimal plays were mega-ghosts (1–5 drafts, boost 2.5–3.0x). Five bugs caused this systematic failure:

**Bug 1 — `is_most_drafted_3x` never fired on live slates.**
The DB flag is set retrospectively only. For today's slate, all players had `is_most_drafted_3x=False`, so the V2.3 env-aware trap penalty (0.60–0.80x) never fired. **Fix (`app/routers/filter_strategy.py`):** Dynamically compute after candidate resolution — top-5 most-drafted players with `boost >= 3.0` are marked `is_most_drafted_3x = True` each run. Constant: `MOST_DRAFTED_3X_TOP_N = 5`.

**Bug 2 — EV floor was completely ineffective (double failure).**
`GHOST_BOOST_EV_FLOOR_SCORE = 18.0` was below the natural ghost trait score (~29) → floor never activated. The floor also applied `_graduated_env_penalty()`, which meant floor and natural value shared the same penalty → floor could never exceed natural. **Fix (`app/services/filter_strategy.py`, `app/core/constants.py`):**
- Raised `GHOST_BOOST_EV_FLOOR_SCORE` 18 → **30**
- Expanded condition: `boost >= 3.0 + drafts < 50` → `boost >= 2.5 + drafts < 100`
- Removed env penalty from floor calculation — data scarcity suppresses env_score too (unknown batting order drops env 0.5 → 0.17), so applying env to the floor double-penalises the same gap

**Bug 3 — Mega-ghost synergy bonus (1.50×) gated on `env_score >= 0.5`.**
Mega-ghosts rarely pass env due to data sparsity. **Fix:** Removed env requirement for `drafts < 50 + boost >= 3.0`. Historical 82% win rate makes env gating counterproductive for this tier. Standard ghost-boost synergy (drafts < 200, boost ≥ 2.5) still requires env pass.

**Bug 4 — Ghost enforcement swap threshold (70%) too strict.**
`_validate_lineup_structure` only forced ghost inclusion if `best_ghost.filter_ev >= worst.filter_ev × 0.70`. With env penalty crushing ghost EV and bonuses blocked, ghosts scored ~50–60% of chalk → swap never triggered. **Fix:** Lowered to `GHOST_ENFORCE_SWAP_THRESHOLD = 0.50`. Enforcement fallback now also accepts mega-ghost+boost players when no env-passing ghost is found.

**Bug 5 — Full env penalty applied to mega-ghost-boost despite unreliable env.**
At env=0, a 40% haircut fires for any boosted player — even those whose low env_score is caused by missing batting order data, not a genuinely bad matchup. **Fix:** Added `MEGA_GHOST_ENV_PENALTY_FLOOR = 0.80`: worst-case env haircut capped at 20% for `drafts < 50 + boost >= 3.0`.

**April 8 ghost+boost winners (illustrating the corrected edge):**
| Player | Drafts | Boost | RS | TV |
|---|---|---|---|---|
| Angel Martínez (CLE,SS) | 2 | +3.0x | 6.9 | 34.5 |
| Rafael Devers (STL,3B) | 4 | +3.0x | 5.1 | 25.5 |
| Taylor Ward (BOS,OF) | 2 | +2.5x | 5.1 | 23.0 |
| Alec Burleson (STL,OF) | 5 | +2.8x | 4.6 | 22.1 |
| Edouard Julien (MIN,2B) | 1 | +3.0x | 3.6 | 18.0 |

**April 7 ghost+boost winners (confirmed edge):**
| Player | Drafts | Boost | RS | TV |
|---|---|---|---|---|
| Amed Rosario (MIL,SS) | 1 | +3.0x | 7.3 | 36.5 |
| Willi Castro (MIN,OF) | 4 | +3.0x | 5.4 | 27.0 |
| Curtis Mead (TB,3B) | 4 | +3.0x | 4.8 | 24.0 |
| Pete Crow-Armstrong (CHC,OF) | 26 | +3.0x | 4.0 | 20.0 |

### V2.3 Changes (April 8 Post-Mortem — April 7 slate)

**April 7 results:** User scored 32.37 (2623rd of 14.8k, top 20%). Rank 1 scored 71.08.

1. **Env-aware 3x trap penalty** — `_compute_filter_ev()` applies `MOST_DRAFTED_3X_ENV_PASS_PENALTY = 0.80` (20% haircut) when env passes, vs `MOST_DRAFTED_3X_PENALTY = 0.60` (40% haircut) when it doesn't. Moonshot always applies the full 40%.

2. **Max 1 pitcher per lineup** — `_validate_lineup_structure()` enforces `MAX_PITCHERS_IN_LINEUP = 1`. Excess pitchers replaced by highest-EV non-pitcher.

**Boost traps avoided by 3x-env rule:**
| Player | Drafts | Boost | RS | Env |
|---|---|---|---|---|
| José Ramírez (CLE,3B) | 362 | +3.0x | -0.7 | fail |
| Aaron Judge (NYY,OF) | 1800 | +1.9x | -0.2 | fail |
| Yordan Alvarez (HOU,DH) | 2300 | +1.6x | -0.4 | fail |
| Tarik Skubal (DET,P) | 4400 | none | 0.7 | fail |

### V2.2 Changes (April 8 Post-Mortem — April 6 slate)

1. **Graduated score penalty** — linear from 0.40x (score=0) to 1.0x (score=15+). See `_graduated_score_penalty()`.
2. **Graduated env penalty** — linear from 0.60x (env=0) to 1.0x (env=0.5+). See `_graduated_env_penalty()`.
3. **Ghost-boost EV floor** — originally set at score=18 (proved ineffective; fixed in V2.4). See `_apply_ghost_boost_ev_floor()`.
4. **K/9 reverse-engineering fix** in `app/routers/filter_strategy.py` — `6.0 + (score/max × 6.0)`.

**Old constants removed:** `MIN_SCORE_PENALTY`, `BOOST_NO_ENV_PENALTY` (replaced by `MIN_SCORE_PENALTY_FLOOR`, `BOOST_NO_ENV_PENALTY_FLOOR`).

### V3.0 Changes (April 11 — Game Theory & Probabilistic Architecture)

**Philosophy shift:** From heuristic rule engine to probabilistic options-pricing model. The system now treats DFS picks as options contracts: a +3.0x boost is an "in-the-money" option (low strike price), a 0.0x boost is "at-the-money," and the crowd's information asymmetry is the "implied volatility."

**Pillar 1 — Bayesian Dead Capital (`app/services/condition_classifier.py`)**
- Replaced DEAD_CAPITAL hard-blocks (returned 0.0) with Laplace-smoothed Bayesian floors.
- Uses Beta-Binomial conjugate prior (alpha=1, beta=1): `posterior = (successes + 1) / (trials + 2)`.
- 0/34 observations → floor of 0.028 (not 0.0). 0/8 → floor of 0.10.
- Added `CONDITION_OBSERVATIONS` and `PITCHER_CONDITION_OBSERVATIONS` matrices tracking (successes, trials) per cell for principled updating.
- `LEGACY_DEAD_CAPITAL_CONDITIONS` retained for logging/reference only.
- ML model (`ml_model.py`) can now contribute signal for all conditions — the `matrix_rate == 0.0` early-return is removed.

**Pillar 2 — Bifurcated Environmental Module (`app/services/filter_strategy.py`)**
- `compute_batter_env_score()` now returns `(env_score, factors, unknown_count)` — tracking how many environmental factors were missing (None) vs. confirmed bad.
- Three-tier DNP handling replaces the single `DNP_RISK_PENALTY = 0.70`:
  - `DNP_RISK_PENALTY = 0.70`: Confirmed bad (lineup published, player absent) — 30% haircut
  - `DNP_UNKNOWN_PENALTY = 0.85`: Unknown (lineup not published, many missing factors) — 15% haircut
  - `DNP_GHOST_UNKNOWN_PENALTY = 0.92`: Ghost unknown (data scarcity expected) — 8% haircut
- `FilteredCandidate` carries `env_unknown_count` for downstream use.
- Ghost players with missing batting orders no longer receive the same penalty as chalk players with published lineups.

**Pillar 3 — Percentile-Based Ownership Tiers (`app/services/condition_classifier.py`)**
- `get_ownership_tier()` now uses empirical CDF percentiles as the primary path (when slate distribution is available):
  - Ghost: bottom 15%, Low: 15-35%, Medium: 35-65%, Chalk: 65-90%, Mega-chalk: top 10% + drafts > 3x median
- Absolute draft-count thresholds (`GHOST_DRAFT_THRESHOLD = 100`, etc.) are fallbacks only.
- Mega-chalk requires both percentile rank AND absolute floor (`MEGA_CHALK_MEDIAN_MULTIPLE = 3.0`) to prevent false positives on thin slates.
- `most_drafted_3x` now scales with slate size: top 30% of the 3x-boost pool, clamped [3, 7].
- Meta-game monitoring: `compute_draft_entropy()` and `compute_gini_coefficient()` logged per slate. Sustained entropy increase = ghost edge compression warning.

**Pillar 4 — Dynamic Pitcher Cap (`app/services/filter_strategy.py`)**
- `compute_dynamic_pitcher_cap()` replaces the rigid `MAX_PITCHERS_IN_LINEUP = 1`.
- Rich boosted pool (>= 5 quality cards): cap at 1 pitcher (ghost+boost batter edge dominates).
- Thin boosted pool (< 5 quality cards): cap at 2 pitchers (unboosted SPs have 93% positive RS, avg 5.4).
- Applied independently to Starting 5 and Moonshot lineups.
- `_enforce_composition()` and `_validate_lineup_structure()` accept `pitcher_cap` parameter.

**New constants (`app/core/constants.py`):**
- `DNP_UNKNOWN_PENALTY = 0.85`, `DNP_GHOST_UNKNOWN_PENALTY = 0.92`
- `OWNERSHIP_PERCENTILE_GHOST = 0.15`, `OWNERSHIP_PERCENTILE_LOW = 0.35`, `OWNERSHIP_PERCENTILE_MEDIUM = 0.65`, `OWNERSHIP_PERCENTILE_CHALK = 0.90`
- `MEGA_CHALK_MEDIAN_MULTIPLE = 3.0`
- `MOST_DRAFTED_3X_MIN_N = 3`, `MOST_DRAFTED_3X_MAX_N = 7`, `MOST_DRAFTED_3X_PROPORTION = 0.30`
- `MAX_PITCHERS_THIN_POOL = 2`

### V3.2 Changes (April 11 — Cross-Lineup Correlation & Within-Tier Differentiation)

**Context**: April 10th simulation showed optimizer captures ~10 of top 17 performers. Root causes: (a) all ghost+max_boost candidates have identical condition_hv_rate=1.00, leaving within-tier differentiation to noisy rs_prob alone; (b) ghost+mid_boost players (HV rate 0.75) blocked by binary 2.5 threshold; (c) no mechanism to capture correlated upside across two lineups when MAX_PLAYERS_PER_TEAM=1.

**Change 1 — MAX_PLAYERS_PER_TEAM=1, MAX_PLAYERS_PER_GAME=1** (`app/core/constants.py`)
- Hard constraint: max 1 player per team per individual lineup (Starting 5 or Moonshot).
- MAX_PLAYERS_PER_GAME lowered from 3 to 2 (V3.2), then to 1 (V3.3) — full game diversification on large slates.
- Within-lineup stacking disabled. Correlation captured cross-lineup via Change 3.
- _build_team_stack() path in _enforce_composition() auto-skipped (MAX_PLAYERS_PER_TEAM < STACK_MIN_PLAYERS).

**Change 2 — Three-Tier Lineup Construction: auto → soft_auto → rest** (`app/services/condition_classifier.py`, `app/services/filter_strategy.py`)
- New `is_soft_auto_include()`: ghost tier + boost >= 2.0 (but < 2.5).
- Ghost+mid_boost historical HV rate = 0.75 — excellent but below auto-include's 0.88-1.00.
- Three-tier ordering in `_enforce_composition()`: auto-include fills first, then soft_auto, then rest by filter_ev.
- Captures James Wood (Apr 10: 52 drafts, 2.0x, TV 16.8) who was missed by the binary 2.5 threshold.
- New constant: `SOFT_AUTO_INCLUDE_BOOST_THRESHOLD = 2.0`.

**Change 3 — Cross-Lineup Correlation Awareness** (`app/services/filter_strategy.py`)
- `_identify_correlation_groups()`: finds teams with 2+ ghost players.
- `correlation_bonus` field on FilteredCandidate, set before EV computation.
- Ghost players on correlation teams get +10% EV (2 ghosts) or +15% EV (3+ ghosts).
- Moonshot: ghost teammate of a Starting 5 player gets +20% BONUS (replaces -15% penalty).
- Example: TOR has Vlad (56 drafts) + Schneider (7 drafts) → S5 gets one, Moonshot gets the other.
- New constants: `CORRELATION_GHOST_MIN_PLAYERS=2`, `CORRELATION_EV_BONUS=1.10`, `CORRELATION_EV_BONUS_3PLUS=1.15`, `MOONSHOT_CORRELATION_TEAMMATE_BONUS=1.20`.

**Change 4 — Environmental Tiebreaker for Auto-Include Tier** (`app/services/filter_strategy.py`)
- When condition_hv_rate >= 0.85, add up to +15% EV based on env_score.
- Differentiates among ghost+max_boost candidates: confirmed lineup spot at Coors > unknown order at Petco.
- Applied in both `_compute_filter_ev()` and `_compute_moonshot_filter_ev()`.
- New constants: `ENV_TIEBREAKER_BONUS_MAX=0.15`, `ENV_TIEBREAKER_HV_THRESHOLD=0.85`.

**Change 5 — Ghost+Boost Pitcher Cap Expansion** (`app/services/filter_strategy.py`)
- `compute_dynamic_pitcher_cap()`: when ghost+boost pitcher exists (drafts < 100, boost >= 2.5), cap raised to 2 even in rich batter pools.
- Captures Walker Buehler-type plays (low-tier pitcher with max boost, TV 29.5).
- Without this, ghost+boost pitchers lose to ghost+boost batters and get excluded.

### V3.1 Changes (April 11 — Empirical Calibration from April 6-9 Data)

**Fix 1 — Pitcher Exemption for Most-Drafted-3x Trap** (`app/routers/filter_strategy.py`)
- The 57% bust rate for most-drafted 3x batters does NOT apply to starting pitchers.
- Historical evidence: Mick Abel (Apr 9, TV 23.0), Eovaldi (Apr 7, in 11/12 top lineups).
- Pitchers inherently control their own environment — the "crowd is wrong about boost" thesis doesn't transfer.
- `is_most_drafted_3x` flag now only applied to batters. Pitchers with 3x boost are never flagged.

**Fix 2 — Stacking Re-enabled** (`app/core/constants.py`, `app/services/filter_strategy.py`)
- V3.1: `MAX_PLAYERS_PER_GAME` raised from 1 to 3. `MAX_PLAYERS_PER_TEAM` raised from 1 to 3.
- **Superseded by V3.2/V3.3**: `MAX_PLAYERS_PER_TEAM` lowered back to 1, `MAX_PLAYERS_PER_GAME` to 2 (V3.2), then to 1 (V3.3). Within-lineup stacking disabled. Correlation value now captured cross-lineup via `CORRELATION_*` constants. See V3.2 Changes above.
- `MAX_OPPONENTS_SAME_GAME = 1` remains — prevents negative correlation (opposing pitcher + batters).
- Team-aware game diversification: the selection loop tracks `(game_id, team)` tuples, not just `game_id`.

**Fix 3 — Ghost Absolute Draft Floor** (`app/services/condition_classifier.py`)
- Added `GHOST_ABSOLUTE_DRAFT_FLOOR = 25`: players with ≤25 drafts are always classified ghost.
- Prevents the zero-draft CDF trap: when 30-40% of the pool has 0 drafts, the 15th percentile is 0, pushing mega-ghosts (1-2 drafts) into "low" tier.
- Applied BEFORE the percentile check as an `OR` condition.

**Fix 4 — Env-Scaled Unboosted Pitcher Penalty** (`app/services/filter_strategy.py`)
- `UNBOOSTED_PITCHER_RICH_POOL_PENALTY` now scales with `env_score`:
  - env=0.0 → 0.65 (35% haircut, same as V2)
  - env=0.5 → 0.775 (22% haircut)
  - env=1.0 → 0.90 (10% haircut — ace with perfect environment barely penalized)
- New constant: `UNBOOSTED_PITCHER_RICH_POOL_PENALTY_CEIL = 0.90`.
- Historical counter-examples: Nolan McLean (Apr 9, 0 boost, biggest overperformer), Sandy Alcantara (Apr 7, RS 7.5).

### Lineup Construction (V3.2 — Three-Tier EV, dynamic pitcher cap)
Historical data (13 rank-1 winners): avg 2.15 pitchers, range 0-5. Dynamic cap: 1 SP when boosted pool is rich (V3.2: 2 SP if ghost+boost pitcher exists), 2 SP when thin. **No "day types" force composition.**

1. **Three-tier ordering**: auto-include (ghost+boost ≥ 2.5) → soft_auto_include (ghost+boost ≥ 2.0) → rest by filter_ev
2. **MAX_PLAYERS_PER_TEAM = 1, MAX_PLAYERS_PER_GAME = 1**: No within-lineup stacking, no same-game pairing. Each of the 5 players comes from a different game. Correlation captured cross-lineup.
3. **Blowout game detected**: Stack path skipped (requires MAX_PLAYERS_PER_TEAM ≥ 3, currently 1).
4. **All slates**: Pure three-tier EV ranking with team cap (1), game cap (1), pitcher cap.

### Lineup Validation (V3.2)
- Max 1 mega-chalk (top 10% percentile + 3x median drafts) player
- Min 1 ghost (bottom 15% percentile) player: first seek env-passing ghost, fallback to mega-ghost+3x boost even without env pass
- **Max 1 player per team** per individual lineup (V3.2)
- **Max 1 player per game** per individual lineup (V3.3) — each pick from a distinct game
- **Dynamic pitcher cap** (1 or 2) — V3.2: 2 allowed when ghost+boost SP exists
- Slot 1 Differentiator: swap consensus Slot 1 for contrarian if EV loss <10%

### Slate Classification (informational only — does NOT force composition)
- Classification exists for blowout detection and display only
- **No slate type forces pitcher/hitter counts.** `SLATE_COMPOSITION` dict was removed in V2.1.

**Moonshot** — Completely different 5 players. Heavier anti-crowd lean:
- FADE=0.60, NEUTRAL=0.95, TARGET=1.30
- Sharp signal bonus: up to +25% EV from underground buzz
- Explosive bonus: up to +10% EV from power_profile (batters) or k_rate (pitchers)
- Game diversification: 0.85x soft penalty for same-team overlap with Starting 5, EXCEPT ghost teammates on correlation teams (2+ ghosts same team) get +20% BONUS instead (V3.2)
- Zero player overlap with Starting 5
- All V2 penalties (most_drafted_3x, mega-chalk, ghost+boost synergy) apply

**Key functions (filter_strategy.py):**
- `run_filter_strategy()` — Starting 5
- `run_dual_filter_strategy()` — One call, two lineups (V3.2: cross-lineup correlation)
- `_compute_base_ev()` — DRY: shared 4-term formula (condition_hv_rate × rs_prob × stack × crowd × dnp × env_tiebreaker)
- `_compute_filter_ev()` — Starting 5 EV (delegates to `_compute_base_ev`)
- `_compute_moonshot_filter_ev()` — Moonshot EV (delegates to `_compute_base_ev` + sharp/explosive bonuses)
- `_compute_dnp_adjustment()` — DRY: bifurcated DNP risk (ghost/unknown/confirmed)
- `_apply_unboosted_pitcher_penalty()` — DRY: env-scaled pitcher haircut (shared by S5 and Moonshot)
- `_identify_correlation_groups()` — V3.2: finds teams with 2+ ghost players for cross-lineup distribution
- `compute_dynamic_pitcher_cap()` — V3.2: allows 2 pitchers when ghost+boost SP exists
- `_build_team_stack()` — Ghost-pool team stacking (V3.2: skipped when MAX_PLAYERS_PER_TEAM=1)
- `_enforce_composition()` — V3.2: three-tier (auto/soft_auto/rest) with team cap=1
- `_validate_lineup_structure()` — Max 1 mega-chalk, min 1 ghost (V2.4: fallback to mega-ghost+3x)
- `_smart_slot_assignment()` — Slot sequencing (unboosted first)

**Key functions (condition_classifier.py):**
- `is_auto_include()` — Ghost + boost >= 2.5 (primary edge)
- `is_soft_auto_include()` — V3.2: Ghost + boost >= 2.0 (second tier, HV rate 0.75)

**Key functions (routers/filter_strategy.py):**
- `_resolve_candidates()` — Builds candidate pool; V2.4 dynamically sets `is_most_drafted_3x` for top-5 boost=3.0 players by draft count

**Dead code:** `app/services/draft_optimizer.py` — functions are not wired to any router except `evaluate_lineup`. The filter_strategy path supersedes it entirely.

## API Structure (8 routers under `/api/`)

| Router | Prefix | Purpose |
|---|---|---|
| filter-strategy | `/api/filter-strategy` | PRIMARY: Dual-lineup optimization (Starting 5 + Moonshot) |
| players | `/api/players` | Player CRUD + search |
| slates | `/api/slates` | Slate management + draft cards + results |
| scoring | `/api/score` | On-demand scoring + rankings |
| draft | `/api/draft` | Lineup evaluation only (no optimize endpoint) |
| calibration | `/api/calibration` | Scoring weight configuration |
| pipeline | `/api/pipeline` | Orchestrated fetch → score → rank |
| popularity | `/api/popularity` | Player/slate popularity analysis |

## Core Rules & Business Logic

1. **Sport-Specific:** This is MLB only. Do NOT add NBA/NFL/etc. logic.
2. **No fallbacks ever.** See "ABSOLUTE RULE" section above.
3. **total_value is absolute:** Always `real_score * (2 + card_boost)`. Never null.
4. **Enrichment:** Real Sports data does NOT provide Team or Position. The seed script and AI must append standard 3-letter MLB team abbreviations and positions.
5. **Volume:** Ownership volume uses `drafts` column with boolean flags (`is_most_popular`, `is_highest_value`, `is_most_drafted_3x`). Note: `is_most_drafted_3x` is retrospective in the DB — the optimizer recomputes it dynamically each run (top-5 most-drafted with boost ≥ 3.0) so the V2.3 trap penalty fires for live slates.
6. **DRY:** The total_value formula, player lookups, score queries, and game log sorting are centralized in `app/core/utils.py`.
7. **is_highest_value / is_most_popular flags are retrospective labels.** Never use them as inputs to prediction or optimization — that is a data leak. They reflect post-hoc outcomes only.

## Strategy: V2 "Anchor, Differentiate, Stack" (Master Strategy Document)

Full document (V2) is the authoritative reference. Key mechanics for any AI working on this codebase:

### The Formula is Additive (Proven)
```
Player Slot Value = RS × (slot_multiplier + card_boost)
```
Not multiplicative. Proven from historical data. This means:
- Unboosted player: Slot 1 → Slot 5 = **67% value loss** (2.0x → 1.2x)
- 3.0x boosted player: Slot 1 → Slot 5 = **16% value loss** (5.0x → 4.2x)
- Implication: unboosted players MUST go in Slot 1. Boosted players are slot-flexible.

### The Five Filters (Sequential)

**Filter 1 — Slate Classification** (informational only — does NOT force composition)
- Tiny (1-3 games): candidate for blowout stacking (if detected)
- Pitcher Day (4+ quality SP matchups): indicates pitcher environmental advantage
- Hitter Day (5+ games with O/U ≥ 9.0): indicates hitter environmental advantage
- Standard (10+ games, mixed): no special classification
- Classification is used for blowout detection and display only. Pure EV ranking determines actual composition. `SLATE_COMPOSITION` was removed in V2.1.

**Filter 2 — Environmental Advantage** (pre-game data only)
- Pitchers: weak opponent (OPS < .700), high K/9 (≥ 8.0), pitcher-friendly park, home field
- Batters: high Vegas total (O/U ≥ 8.5), weak opposing starter (ERA ≥ 4.5), platoon advantage, batting 1-4, hitter-friendly park
- env_score > 0.5 = passes. Stored on SlatePlayer. SlateGame holds the raw data (vegas_total, home/away_starter_era, etc.)
- If a field is NULL (data not yet available), scoring defaults to neutral — not fabricated

**Filter 3 — Ownership Leverage**
- FADE = crowd has found this player. 25% EV penalty (Moonshot: 40%).
- TARGET = crowd is ignoring this player. 15% EV bonus (Moonshot: 30%).
- Ghost players (< 100 drafts) with high boost are the highest-EV pool (82% TV>15 rate historically).
- `is_most_drafted_3x` — dynamically computed each run: top-5 most-drafted players with boost ≥ 3.0. These get 20–40% EV penalty (env-aware). Historical bust rate: 57%.
- Historical: most-drafted players chronically underperform. The crowd chases names.

**Filter 4 — Boost Optimization**
- Boost is a multiplier on an unknown outcome — it amplifies downside equally.
- card_boost ≥ 1.0 with env_score < 0.5 → **graduated** EV penalty (V2.2: 0% at env=0.5, up to 40% at env=0.0)
- **Mega-ghost-boost (drafts < 50, boost ≥ 3.0):** env penalty capped at 20% (`MEGA_GHOST_ENV_PENALTY_FLOOR`), synergy bonus (1.50×) applied WITHOUT env requirement, EV floor at score=30 (env-independent). Data scarcity makes env_score unreliable for these players.
- **Ghost+boost floor (boost ≥ 2.5, drafts < 100):** EV floor at score=30, no env penalty on the floor.
- Never assign a boost without environmental support — except for mega-ghost tier where env_score is suppressed by data scarcity rather than bad conditions.

**Filter 5 — Slot Sequencing**
- Unboosted players → highest available slots (67% loss if misplaced)
- Boosted players → fill remaining slots (only 16% loss at max boost)
- Slot 1 Differentiator: when the field converges on an obvious Slot 1 (high-ownership player), the winning move is to put the contrarian play in Slot 1.

### Dynamic Composition: Boost Drives Position Mix
Starting pitchers typically receive 0.0 card_boost (the app doesn't boost them because they get more plays). This means composition should be driven by **boost availability**, not fixed position counts:

- **Rich boosted pool** (5+ quality boosted cards with env support): Pure EV ranking determines composition. No positional constraints. Historical data from 4/2 onward shows zero unboosted pitchers in rank-1 lineups when quality boosted alternatives existed.
- **Thin boosted pool** (< 5 quality boosted cards): Slate-type composition guides backfill. Unboosted pitchers have the highest RS floor (93% positive, avg RS 5.4 in winning lineups) and are the best unboosted option.
- **Boosted pitchers are elite**: When pitchers DO get boosts, they combine high RS floor with boost amplification (e.g., Cole Ragans +3.0 → TV 26.5, Slade Cecconi +3.0 → TV 31.5). Treat them like any other boosted card.

Key constants: `BOOST_QUALITY_THRESHOLD` (1.0) and `BOOSTED_POOL_FULL_THRESHOLD` (5) in `app/core/constants.py`.

### The Ghost Player Edge
The single most consistent edge: players with < 100 drafts with high boost. Historical data across 15 dates: **82% of mega-ghost+boost players (< 50 drafts, boost ≥ 2.0) deliver TV > 15**, vs 0–12% for medium/chalk-tier players with the same boost. Trait scores are unreliable for data-scarce ghost players — use the draft tier as the primary signal, not the score.

Examples: Miguel Vargas (1 draft, RS 6.2), Colson Montgomery (5 drafts, RS 6.3), Oneil Cruz (2 drafts, RS 5.7), Angel Martínez (2 drafts, RS 6.9), Edouard Julien (1 draft, RS 3.6). The crowd chases Ohtani/Judge/Soto regardless of conditions — those three are chronically over-drafted.

### Debut/Return Premium
First MLB game or return from 30+ day absence = near-zero ownership + historically elite RS. Always flag `is_debut_or_return = True` when known. 15% EV bonus applied.

### The Boost Trap (Historical Disasters)
| Date | Player | Boost | Drafts | RS | total_value |
|---|---|---|---|---|---|
| 4/3 | Michael Lorenzen | 3.0 | 674 | -6.4 | **-32.0** |
| 4/1 | Shane Smith | 3.0 | 1,300 | -3.5 | **-17.5** |
| 3/30 | Shohei Ohtani | 3.0 | 4,400 | 0.0 | 0.0 |

Boost amplifies negative RS just as aggressively as positive RS. Never boost without environmental support.

### Team Stacking (Condition E)
When one hitter on a team has a big game, teammates follow (runs require baserunners). Historical winning lineups exploit this (MIL stack 3/28, ATL stack 4/2, NYY stack 3/25). The optimizer does NOT explicitly enforce team stacking — this is intentional (the environmental filter naturally surfaces the best team). Do not add stacking as a hard constraint.

## Deployment

- **Dockerfile** + **Procfile** included for Railway
- Environment vars use `DFS_` prefix (see `.env.example`)
- SQLite by default, swap `DFS_DATABASE_URL` for Postgres in production
- Database seeds automatically on startup via FastAPI lifespan
- Startup runs `run_full_pipeline(db, date.today())` as a background task
- If pipeline fails, the app returns a 404 from `/api/filter-strategy/optimize` — **this is correct behavior, not a bug to work around**
