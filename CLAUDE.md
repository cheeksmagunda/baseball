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

Current coverage (as of 2026-04-12): **19 consecutive dates, 2026-03-25 → 2026-04-12**. All four files stay in lockstep — every date present in one is present in all four.

| File | Format | Current size | Purpose |
|---|---|---|---|
| `historical_players.csv` | CSV | 677 rows / 19 dates | Master player ledger (1 row per player/day). Contains total_value, card_boost, drafts, leaderboard flags. **Null `real_score` / `total_value` indicates a DNP / scratch — there is no separate flag.** Avg ~35 rows/date (range 22–56). |
| `historical_winning_drafts.csv` | CSV | 655 rows / 19 dates | Top-ranked lineups per date (5 rows per lineup = one per slot). Collection depth varies by date (4–12 ranks observed); the collector aspires to rank-20 but does not always reach it. |
| `historical_slate_results.json` | JSON array | 19 entries | Per-date MLB slate outcome envelope: `date`, `game_count`, `games[]`, `season_stage`, `source`, `saved_at`, `notes`. One object per slate day. |
| `hv_player_game_stats.csv` | CSV | 290 rows / 19 dates | Actual box score stats for every Highest-Value player appearance to date (grows each slate). Batting columns (ab, r, h, hr, rbi, bb, so) and pitching columns (ip, er, k_pitching, decision) coexist in one table — blanks indicate the column does not apply to that player. |

## Ingesting New Slate Data

New slates are ingested **manually by appending rows** to the four files above — there is no automated collector. After a slate completes, capture the platform's leaderboards and append to each file. The canonical column-by-column reference lives in `.claude/hooks/session-start.sh` (reproduced below). Keep all four files in lockstep — a date missing from any one of them will break cross-validation.

### Per-slate ingest checklist

- [ ] Append player rows to `historical_players.csv`
  - Columns: `date, player_name, team, position, real_score, card_boost, drafts, total_value, is_highest_value, is_most_popular, is_most_drafted_3x`
  - One row per player per slate day. Most Popular + Most Drafted 3x leaderboards are mandatory; Highest Value is optional (set `is_highest_value=1` only if captured).
  - A player in multiple leaderboards = one row with multiple flags set.
  - `total_value = real_score × (2 + card_boost)` — verify manually for each row.
  - `card_boost` blank if no boost card; `"—"` in the UI = 0.0.
  - `drafts`: total draft count shown on the platform (e.g., "1.5k" → 1500).
- [ ] Append winning-lineup rows to `historical_winning_drafts.csv`
  - Columns: `date, winner_rank, slot_index, player_name, team, position, real_score, slot_mult, card_boost`
  - 5 rows per lineup (one per slot 1–5). Target top-20 ranks → 100 rows/day, but capture what is available.
  - `slot_mult`: 2.0 (slot 1), 1.8, 1.6, 1.4, 1.2 (slot 5).
- [ ] Append slate envelope to `historical_slate_results.json`
  - Top-level array — push one object per slate day.
  - Required fields: `date`, `game_count`, `games` (may be `[]`), `season_stage` ("regular-season"), `source` ("screenshot_ingest"), `saved_at` (ISO timestamp), `notes` (free text capturing ghost wins, boost traps, 3x busts, crowd overreactions — the V2 strategy validation raw material).
  - Per-game shape when scores are captured: `{"home": "NYY", "away": "BOS", "home_score": 5, "away_score": 2, "winner": "NYY", "loser": "BOS", "winner_score": 5, "loser_score": 2}`.
- [ ] Append HV box-score rows to `hv_player_game_stats.csv`
  - Columns: `date, player_name, team_actual, position, real_score, card_boost, game_result, ab, r, h, hr, rbi, bb, so, ip, er, k_pitching, decision, notes`
  - One row per Highest-Value-leaderboard player appearance. Batters fill the `ab…so` columns; pitchers fill `ip/er/k_pitching/decision`. Leave non-applicable columns blank.
  - `game_result`: free-form ("SF 0 NYY 7"). `notes`: short summary ("2-for-3 | vs SF (away)").

### Platform → CSV column mapping (historical_players.csv)

| Platform table | "Value" (1st) | "Multiplier" | "Drafts" | "Value" (2nd) |
|---|---|---|---|---|
| Most Popular | `real_score` | `card_boost` | `drafts` | — |
| Highest Value | `real_score` | `card_boost` | `drafts` (HV leaderboard count) | `total_value` (verify vs formula) |
| Most Drafted 3x | `real_score` | `card_boost` | `drafts` | — |

### Reloading the database after ingest

The CSV/JSON files are the source of truth; the SQLite DB is rebuilt from them via `app/seed.py`. `run_seed()` is **idempotency-guarded on an empty DB** — it only seeds if `players` is empty. To pick up freshly appended rows:

```bash
rm db/baseball.db              # or DROP TABLE in Postgres
python -m app.seed             # re-seeds from /data/
```

On Railway, the seed runs automatically via the FastAPI lifespan hook on a fresh DB. There is no incremental seeder — append-and-reseed is the supported workflow.

### Example rows

```
# historical_players.csv
2026-04-09,Aaron Judge,NYY,OF,-0.7,2.3,3900,-3.01,0,1,0
2026-04-09,Mick Abel,BAL,P,4.6,3.0,1700,23.0,0,1,1

# historical_winning_drafts.csv
2026-04-09,1,1,Mick Abel,BAL,P,4.6,2.0,3.0
2026-04-09,1,2,Seth Lugo,KC,P,4.1,1.8,0.0

# hv_player_game_stats.csv
2026-03-25,Austin Wells,NYY,C,1.2,,SF 0 NYY 7,3.0,1.0,2.0,0.0,0.0,1.0,0.0,,,,,2-for-3 | vs SF (away)
```

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

**Strategy Version: V5.0 "Pitcher-Anchor Rule"** — V5.0: every lineup is exactly 1 SP + 4 batters. The highest-EV pitcher is pinned to Slot 1 (2.0x multiplier) and the pitcher's game_id is blocked for all batter picks (no negative correlation). Supersedes the V3.x dynamic pitcher cap and the Slot 1 Differentiator contrarian swap. Starting 5 and Moonshot each anchor on their own best pitcher; Moonshot's pitcher must differ from Starting 5's (player-overlap exclusion).

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

### EV Formula (V3.4 — 4-term condition-based, single source: `_compute_base_ev()`)
```
filter_ev = condition_hv_rate                    # Term 1: from CONDITION_MATRIX (primary signal)
  × rs_prob                                       # Term 2: P(RS >= 15/(2+boost)) from traits
  × stack_bonus (1.20 if blowout game, else 1.0)  # Term 3: blowout team bonus
  × anti_crowd (FADE: pitcher=0.85/batter=0.75, TARGET=1.15) # Term 4: V3.4 pitcher-aware
  × debut_bonus (1.15 if first appearance)
  × dnp_adj (ghost=0.92, unknown=0.85, confirmed_bad=0.70)
  × env_tiebreaker (up to +15% for HV rate ≥ 0.85) # V3.2: differentiates among auto-includes
  × draft_scarcity_tiebreaker (up to +10% for ultra-low drafts) # V3.4: within auto-include tier
  × correlation_bonus (1.10-1.15 for correlation teams) # V3.2: cross-lineup correlation
  × 100
```
Note: card_boost is NOT multiplied again — it's already captured in condition_hv_rate (ghost+3.0x → HV rate 1.00) and rs_prob (higher boost → lower RS threshold).

Post-EV adjustments (applied in `run_filter_strategy`):
- Three-tier ordering for batters: auto-include → soft_auto_include → rest by filter_ev
- **V5.0 pitcher anchor**: exactly 1 pitcher per lineup, pinned to Slot 1. The highest-EV pitcher in the pool is chosen; its `game_id` is added to a blocked set so no batter from the same game is drafted.
- V4.1: Unboosted pitcher penalty removed — matrix now encodes empirical HV rates per (ownership × boost) cell, so stacking a second penalty double-counted

### V5.0 Changes (April 13 — Pitcher-Anchor Rule)

**Design change:** Every lineup (both Starting 5 and Moonshot) is now structurally fixed at **exactly 1 SP + 4 batters**, with the pitcher pinned to **Slot 1 (2.0x multiplier)**. The V3.x dynamic pitcher cap (1/2/3 based on boosted-pool richness) and the Slot 1 Differentiator contrarian swap are both retired.

**Rationale (user directive):**
- Every draft anchors on a pitcher in the 2.0x primary slot. The best-conditions pitcher gets the top multiplier regardless of whether they are boosted or unboosted.
- Batters and pitchers should not compete against each other within a lineup — block the pitcher's `game_id` so no batter in that game (teammate or opponent) can be drafted.
- Starting 5 and Moonshot each anchor on their own best pitcher; Moonshot still excludes Starting 5 player names, which forces a different pitcher in the vast majority of slates.

**Changes:**

1. **New constants (`app/core/constants.py`)**
   - `REQUIRED_PITCHERS_IN_LINEUP = 1` — exactly this many pitchers per lineup
   - `MAX_PITCHERS_IN_LINEUP = 1` — kept identical to REQUIRED for legacy validation paths
   - `PITCHER_ANCHOR_SLOT = 1` — pitcher always goes in Slot 1 (2.0x)
   - **Removed:** `MAX_PITCHERS_THIN_POOL`, `PITCHER_CAP_EV_THRESHOLD`, `BOOSTED_PITCHER_CAP_EXPAND_MIN`, `MAX_PITCHERS_BOOSTED_RICH`, `SLOT1_DIFFERENTIATOR_EV_THRESHOLD`

2. **`compute_dynamic_pitcher_cap()` deleted** (`app/services/filter_strategy.py`)
   - Replaced by a single-pitcher-anchor flow. Both `run_filter_strategy` and `run_dual_filter_strategy` no longer compute or pass a pitcher cap.

3. **`_enforce_composition()` rewritten** — new signature `_enforce_composition(candidates, slate_class)` (no `pitcher_cap` param).
   - **Phase 1:** select the highest-EV pitcher as the anchor. If the pool has no pitcher, raise `ValueError` (no-fallback rule).
   - **Phase 2:** sort batters into three tiers (auto-include → soft_auto → rest by filter_ev).
   - **Phase 3:** fill 4 batter slots while blocking the anchor pitcher's `game_id` (no teammates or opponents in the pitcher's game).
   - Team cap (1) and overall game cap (1) still apply.

4. **`_validate_lineup_structure()` rewritten** — new signature accepts `anchor_pitcher`.
   - Uses `_protected(idx)` helper so the anchor pitcher is exempt from every swap rule (ghost enforcement, mega-chalk cap, team/game caps, unboosted-dominance checks).
   - Ghost-enforcement replacement candidates are filtered to `not c.is_pitcher` and exclude the anchor's game.
   - Final sanity check asserts `pitcher_count_final == REQUIRED_PITCHERS_IN_LINEUP`.

5. **`_smart_slot_assignment()` rewritten** — the anchor pitcher is pinned to `PITCHER_ANCHOR_SLOT` (Slot 1). Batters are distributed across Slots 2–5 with unboosted batters getting the highest available slots (Slot 2 → Slot 5 tail for boosted). The Slot 1 Differentiator contrarian swap is gone — Slot 1 is reserved for the anchor pitcher in every lineup.

**Implications for prior strategy text:**
- Any CLAUDE.md / README statement that the optimizer may produce 0, 2, or 3 pitchers is **obsolete**. The count is now fixed at 1.
- The "unboosted players MUST go in Slot 1" guidance now applies only to batters — and only within Slots 2–5. Slot 1 is the pitcher anchor.
- The "ghost+boost batters outweigh a 2nd pitcher slot" reasoning is no longer a dynamic comparison; the structure is pre-committed.

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

### V3.4 Changes (April 12 — Boosted Pitcher Expansion & Within-Tier Scarcity)

**Context**: April 11th was the worst draft yet. The optimizer selected 5 ghost+3.0x batters (García RS 0, Dingler RS -0.1, Ballesteros RS 1.0, Valenzuela RS -0.4, De La Cruz RS 3.5) — 3 catchers, all busted. Meanwhile, the winning lineups (87.67 score) had 3 chalk pitchers with 3.0x boost (Suarez 2.2k drafts RS 5.7, Sheehan 1.9k RS 2.8, Bassitt 1.5k RS 2.3). Top 6 leaderboard entries ALL had 3+ pitchers.

**Three root causes:**
1. Pitcher cap (max 1-2) blocked chalk+boost pitchers — V3.2 only expanded for ghost+boost pitchers
2. FADE popularity penalty (25%) hit chalk pitchers too hard — pitchers control their own environment, crowd is less wrong about them
3. Zero within-tier differentiation — all ghost+max_boost had identical HV rate 1.00, so 15-draft catchers ranked equal to 1-draft outfielders

**Fix 1 — Boosted Pitcher Cap Expansion** (`app/services/filter_strategy.py`, `app/core/constants.py`)
- `compute_dynamic_pitcher_cap()` now checks ALL boosted pitchers (boost >= 2.5), not just ghost-tier.
- 3+ boosted pitchers → cap = 3 (new `MAX_PITCHERS_BOOSTED_RICH`). Lets EV decide composition.
- 2 boosted pitchers → cap = 2 even with rich batter pool.
- 1 ghost+boost pitcher → cap = 2 (V3.2 preserved).
- Apr 11 had 5 boosted pitchers (Suarez, Sheehan, Bassitt, Lopez, Walker) → cap would be 3 → allows Suarez, Sheehan, Bassitt to compete on EV.
- New constants: `BOOSTED_PITCHER_CAP_EXPAND_MIN=3`, `MAX_PITCHERS_BOOSTED_RICH=3`.

**Fix 2 — Pitcher-Specific FADE Moderation** (`app/services/filter_strategy.py`, `app/core/constants.py`)
- `_popularity_ev_adjustment()` and `_moonshot_popularity_adj()` now accept `is_pitcher` parameter.
- Pitchers classified FADE get 15% haircut (S5) / 30% haircut (Moonshot), vs 25%/40% for batters.
- Rationale: pitchers control their own game — high draft count reflects real ERA/K-rate data, not media hype. Crowd is structurally less wrong about pitchers (one-player dependency vs team context for batters).
- Evidence: Apr 11 Suarez (2.2k drafts, FADE) appeared in 6/6 top lineups with RS 5.7. Apr 7 Eovaldi in 11/12 top lineups. PITCHER_CONDITION_MATRIX chalk+max_boost=0.42 (5× batter chalk+max rate of 0.23).
- New constants: `PITCHER_FADE_PENALTY=0.85`, `MOONSHOT_PITCHER_FADE_PENALTY=0.70`.

**Fix 3 — Draft Scarcity Tiebreaker** (`app/services/filter_strategy.py`, `app/core/constants.py`)
- Within auto-include tier (condition_hv_rate >= 0.85), fewer drafts → small EV bonus (up to +10%).
- Uses log scale for meaningful differentiation: 1 draft → +10%, 5 → +6.5%, 15 → +4.1%, 50 → +1.5%.
- Breaks ties among ghost+max_boost candidates where condition_hv_rate and env_score are similar.
- Apr 11: would have ranked Moniak (1 draft, RS 6.5) above Dingler (15 drafts, RS -0.1).
- Applied alongside existing env_tiebreaker in `_compute_base_ev()`.
- New constant: `DRAFT_SCARCITY_TIEBREAKER_MAX=0.10`.

**Fix 4 — Condition Matrix Updated with April 11 Data** (`app/services/condition_classifier.py`)
- `CONDITION_MATRIX_VERSION` bumped to 1.1, April 11 added to training dates.
- PITCHER_CONDITION_MATRIX updated: mega_chalk+max_boost 0.67 → 0.75 (Suarez HV), chalk+max_boost 0.50 → 0.42 (Sheehan/Bassitt NOT HV), medium+max_boost 0.14 → 0.11 (Walker/Lopez NOT HV).
- PITCHER_CONDITION_OBSERVATIONS updated with 6 new pitcher appearances.

**April 11 data (pitchers that dominated):**
| Player | Drafts | Boost | RS | TV | Tier | HV? |
|---|---|---|---|---|---|---|
| Ranger Suarez | 2,200 | +3.0x | 5.7 | 28.5 | mega_chalk+max | ✓ |
| Emmet Sheehan | 1,900 | +3.0x | 2.8 | 14.0 | chalk+max | ✗ |
| Chris Bassitt | 1,500 | +3.0x | 2.3 | 11.5 | chalk+max | ✗ |
| Max Fried | 3,000 | none | 6.0 | 12.0 | mega_chalk+no | ✗ |

**April 11 data (ghost batters — winners vs our picks):**
| Player | Drafts | Boost | RS | TV | Notes |
|---|---|---|---|---|---|
| Mickey Moniak | 1 | +3.0x | 6.5 | 32.5 | Winner — ultra-ghost |
| Ramón Laureano | 4 | +3.0x | 6.1 | 30.3 | Winner — ultra-ghost |
| Riley Greene | 3 | +3.0x | 5.8 | 28.8 | Winner — ultra-ghost |
| Adolis García | 11 | +3.0x | 0.0 | 0.0 | Our pick — busted |
| Dillon Dingler | 15 | +3.0x | -0.1 | -0.5 | Our pick — busted |
| Brandon Valenzuela | 9 | +3.0x | -0.4 | -2.0 | Our pick — busted |

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
- **Superseded by V4.1**: the penalty has been removed entirely. The V4.0
  condition matrix retrain now encodes empirical HV rates per (ownership ×
  boost) cell directly (e.g. chalk+no_boost pitcher 0.33, mega_chalk+no_boost
  pitcher 0.19). Stacking a second 10–35% haircut on top double-counted the
  unboosted-ness and buried anchor plays like Alcantara/McLean/Fried.

### Lineup Construction (V5.0 — Pitcher-Anchor + Three-Tier Batters)
Every lineup is **exactly 1 SP + 4 batters**. The count is fixed, not data-driven.

1. **Pitcher anchor (Phase 1)**: pick the highest-EV pitcher in the pool as the anchor. Pin to Slot 1 (`PITCHER_ANCHOR_SLOT = 1`, 2.0x multiplier). Add the anchor's `game_id` to a blocked set.
2. **Three-tier batter ordering (Phase 2)**: auto-include (ghost+boost ≥ 2.5) → soft_auto_include (ghost+boost ≥ 2.0) → rest by filter_ev
3. **Fill 4 batter slots (Phase 3)**: honour `MAX_PLAYERS_PER_TEAM = 1`, `MAX_PLAYERS_PER_GAME = 1`, and the blocked pitcher-game. Each batter comes from a distinct game, none in the pitcher's game.
4. **No-fallback rule**: if the pool contains no pitcher, raise `ValueError` — do not substitute.

### Lineup Validation (V5.0)
- Max 1 mega-chalk (top 10% percentile + 3x median drafts) player — anchor pitcher exempt
- Min 1 ghost (bottom 15% percentile) player: first seek env-passing ghost, fallback to mega-ghost+3x boost. Replacement pool is batters-only and excludes the anchor's game. Anchor pitcher is never swapped out.
- **Max 1 player per team** per individual lineup — anchor pitcher exempt
- **Max 1 player per game** per individual lineup — anchor pitcher is the seed, 4 batters come from 4 different non-anchor games
- **Exactly 1 pitcher** (`REQUIRED_PITCHERS_IN_LINEUP = 1`) — enforced by final assertion in `_validate_lineup_structure`
- Slot 1 Differentiator contrarian swap **retired** — Slot 1 is always the anchor pitcher

### Slate Classification (informational only — does NOT force composition)
- Classification exists for blowout detection and display only
- **No slate type forces pitcher/hitter counts.** `SLATE_COMPOSITION` dict was removed in V2.1.

**Moonshot** — Completely different 5 players. Heavier anti-crowd lean:
- Same structural shape as Starting 5: **1 SP anchor in Slot 1 + 4 batters in Slots 2–5** (V5.0)
- The Moonshot anchor is the highest-EV pitcher in the Moonshot pool; since the Moonshot pool excludes Starting 5 player names, this is normally a different pitcher than the Starting 5 anchor
- Moonshot's anchor game_id is blocked for Moonshot batters independently
- FADE=0.60, NEUTRAL=0.95, TARGET=1.30
- Sharp signal bonus: up to +25% EV from underground buzz
- Explosive bonus: up to +10% EV from power_profile (batters) or k_rate (pitchers)
- Game diversification: 0.85x soft penalty for same-team overlap with Starting 5, EXCEPT ghost teammates on correlation teams (2+ ghosts same team) get +20% BONUS instead (V3.2)
- Zero player overlap with Starting 5
- All V2 penalties (most_drafted_3x, mega-chalk, ghost+boost synergy) apply

**Key functions (filter_strategy.py):**
- `run_filter_strategy()` — Starting 5 (V5.0: pitcher-anchor flow, no pitcher cap)
- `run_dual_filter_strategy()` — One call, two lineups (V3.2: cross-lineup correlation; V5.0: each lineup anchored on its own best pitcher)
- `_compute_base_ev()` — DRY: shared 4-term formula (condition_hv_rate × rs_prob × stack × crowd × dnp × env_tiebreaker)
- `_compute_filter_ev()` — Starting 5 EV (delegates to `_compute_base_ev`)
- `_compute_moonshot_filter_ev()` — Moonshot EV (delegates to `_compute_base_ev` + sharp/explosive bonuses)
- `_compute_dnp_adjustment()` — DRY: bifurcated DNP risk (ghost/unknown/confirmed)
- `_identify_correlation_groups()` — V3.2: finds teams with 2+ ghost players for cross-lineup distribution
- `_build_team_stack()` — Ghost-pool team stacking (V3.2: skipped when MAX_PLAYERS_PER_TEAM=1)
- `_enforce_composition()` — **V5.0**: Phase 1 picks highest-EV pitcher as anchor and blocks its `game_id`; Phase 2 applies three-tier (auto/soft_auto/rest) to batters only; Phase 3 fills 4 batter slots honouring team cap=1, game cap=1, and the blocked anchor-game. Raises `ValueError` if pool has no pitcher.
- `_validate_lineup_structure()` — **V5.0**: protects the anchor pitcher via `_protected(idx)`; ghost-enforcement replacements filtered to batters only; final assertion `pitcher_count_final == REQUIRED_PITCHERS_IN_LINEUP`.
- `_smart_slot_assignment()` — **V5.0**: pins pitcher to Slot 1 (`PITCHER_ANCHOR_SLOT`); distributes batters across Slots 2–5 (unboosted first). Slot 1 Differentiator swap removed.
- `compute_dynamic_pitcher_cap()` — **Deleted in V5.0**. Replaced by the hard-coded 1-pitcher anchor flow.

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
- Implication (V5.0): Slot 1 is reserved for the anchor pitcher. Among batters in Slots 2–5, unboosted batters take the highest available slot (Slot 2 first) and boosted batters tail into the lower slots since they are more slot-flexible.

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

**Filter 5 — Slot Sequencing (V5.0 pitcher-anchor)**
- **Slot 1 (2.0x) is always the anchor pitcher.** The Slot 1 Differentiator contrarian swap is retired.
- Slots 2–5 are batters. Among them, unboosted batters take the highest available slots (Slot 2 first) because the additive formula punishes unboosted cards in lower slots; boosted batters tail into the remaining slots (only 16% loss at max boost).

### Fixed Composition (V5.0): 1 SP + 4 Batters, Always
V5.0 replaces the V3.x boost-driven dynamic composition with a hard structural rule: **every lineup is 1 pitcher + 4 batters, and the pitcher is pinned to Slot 1.** The position mix is no longer contingent on how rich the boosted pool is or whether a ghost+boost pitcher exists.

- **Anchor selection**: the highest-EV pitcher in the pool is chosen — boosted or unboosted, ghost or chalk, treated uniformly. Pitchers still benefit from the pitcher-specific FADE moderation (V3.4) and pitcher condition matrix.
- **Batters**: the remaining 4 slots are filled from the three-tier batter ordering (auto → soft_auto → rest). The pitcher's `game_id` is blocked, so no batter in that game — teammate or opponent — may appear.
- **Boosted pitchers**: still elite when they exist (e.g., Cole Ragans +3.0 → TV 26.5). V5.0 changes nothing about their EV — they simply win the anchor slot when their filter_ev is highest.
- **Unboosted pitchers**: historically have the highest RS floor (93% positive, avg RS 5.4 in winning lineups). Under V5.0 they are just as eligible for the anchor slot as any other pitcher.

`BOOST_QUALITY_THRESHOLD` (1.0) and `BOOSTED_POOL_FULL_THRESHOLD` (5) in `app/core/constants.py` remain defined but are no longer consulted for composition decisions — the 1-pitcher rule is pre-committed.

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
