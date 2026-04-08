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

**Strategy Version: V2.2 "Anchor, Differentiate, Stack"** — based on 14 days of historical data (V2.2: April 8 post-mortem fixes).

The active optimizer produces **two lineups** from the same candidate pool via `run_dual_filter_strategy`.

### Three Pillars (V2)

1. **Ghost Ownership (#1 edge):** 12/13 rank-1 lineups had ≥1 ghost player (<100 drafts). Ghost+boost (≥2.5 boost + <200 drafts) is the "holy grail" combo. Mega-ghost+boost (<50 drafts + 3.0 boost + env pass) gets auto-include-level EV bonus.
2. **Team Stacking:** Dominant winning pattern on 62% of days. On hitter/stack days, stack 3-4 from the favored team's ghost pool + 1-2 diversifiers.
3. **Boost Leverage:** "Most drafted at 3x boost" busts 57% of the time — it's a SELL signal. Ghost+boost is the buy signal.

### Starting 5 EV formula (V2.2)
```
base_ev = total_score × (2 + card_boost)
  × graduated_score_penalty (linear: score 0→0.40x, score 15+→1.0x)
  × graduated_env_penalty (if boost ≥ 1.0: linear env 0→0.60x, env 0.5+→1.0x)
  → ghost_boost_ev_floor (if boost 3.0 + <50 drafts: floor at score=18 equivalent)
  × popularity_adj (FADE=0.75, TARGET=1.15)
  × most_drafted_3x_penalty (0.60 if flagged)
  × ownership_adj (ghost=1.25, low=1.10, chalk=0.80, mega_chalk=0.70)
  × ghost_boost_synergy (mega_ghost+boost=1.50, ghost+boost=1.30)
  × debut_return_bonus (1.15 if flagged)
```

### V2.2 Changes (April 8 Post-Mortem)

Three bugs were identified that caused the optimizer to systematically miss ghost+boost top performers:

1. **Graduated score penalty** replaces the binary 50% cliff at score < 15. Now scales linearly from 0.40x (score=0) to 1.0x (score=15+). A player at score=14 now gets ~96% EV instead of 50%. See `_graduated_score_penalty()` in `filter_strategy.py`.

2. **Graduated env penalty** replaces the binary 30% cliff at env < 0.5. Now scales linearly from 0.60x (env=0) to 1.0x (env=0.5+). A player at env=0.48 now gets ~96% EV instead of 70%. See `_graduated_env_penalty()` in `filter_strategy.py`.

3. **Ghost-boost EV floor** ensures mega-ghost-boost players (3.0x boost, <50 drafts) have a minimum base EV equivalent to an 18-score player, regardless of actual trait score. This compensates for the scoring engine's inability to accurately evaluate data-scarce ghost players. See `_apply_ghost_boost_ev_floor()` in `filter_strategy.py`, `GHOST_BOOST_EV_FLOOR_SCORE` in `constants.py`.

4. **K/9 reverse-engineering fix** in `app/routers/filter_strategy.py`. The old formula `(score/max × 12)` ignored the 6.0 K/9 floor in the scoring engine's linear scale, compressing a 10 K/9 pitcher to 8.0 K/9 for env scoring. Fixed to `6.0 + (score/max × 6.0)`.

**Old constants removed:** `MIN_SCORE_PENALTY`, `BOOST_NO_ENV_PENALTY` (replaced by `MIN_SCORE_PENALTY_FLOOR`, `BOOST_NO_ENV_PENALTY_FLOOR`).

### Lineup Construction (Pure EV — no position forcing)
Historical data (13 rank-1 winners): avg 2.15 pitchers, range 0-5. Composition varies wildly — the only constant is that the 5 highest-EV players win. **No "day types" force positions.** `SLATE_COMPOSITION` was removed entirely.

1. **Blowout game detected** (moneyline ≥ -200): Try team stack from ghost pool → fill 1-2 diversifiers from other games
2. **All other slates**: Pure EV ranking, position-agnostic. If 0 pitchers have competitive EV, take none. If 4 do, take 4. EV decides everything.

### Lineup Validation (V2)
- Max 1 mega-chalk (2000+ drafts) player
- Min 1 ghost (<100 drafts) player when available
- Slot 1 Differentiator: swap consensus Slot 1 for contrarian if EV loss <10%

### Slate Classification (informational only — does NOT force composition)
- Classification exists for display and stacking decisions only
- Blowout detection (moneyline ≥ -200) triggers stack-building logic
- **No slate type forces pitcher/hitter counts.** `SLATE_COMPOSITION` dict was removed.

**Moonshot** — Completely different 5 players. Heavier anti-crowd lean:
- FADE=0.60, NEUTRAL=0.95, TARGET=1.30
- Sharp signal bonus: up to +25% EV from underground buzz
- Explosive bonus: up to +10% EV from power_profile (batters) or k_rate (pitchers)
- Game diversification: 0.85x soft penalty for same-team overlap with Starting 5
- Zero player overlap with Starting 5
- All V2 penalties (most_drafted_3x, mega-chalk, ghost+boost synergy) apply

**Key functions (filter_strategy.py):**
- `run_filter_strategy()` — Starting 5
- `run_dual_filter_strategy()` — One call, two lineups
- `_compute_filter_ev()` — Starting 5 EV with all V2.2 filters
- `_compute_moonshot_filter_ev()` — Moonshot-specific EV
- `_graduated_score_penalty()` — V2.2: linear score penalty (replaces binary cliff)
- `_graduated_env_penalty()` — V2.2: linear env penalty (replaces binary cliff)
- `_apply_ghost_boost_ev_floor()` — V2.2: minimum EV for mega-ghost-boost players
- `_build_team_stack()` — Ghost-pool team stacking for hitter/stack days
- `_enforce_composition()` — V2 three-path construction (stack / EV / backfill)
- `_validate_lineup_structure()` — Max 1 mega-chalk, min 1 ghost
- `_smart_slot_assignment()` — Slot sequencing (unboosted first)

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
5. **Volume:** Ownership volume uses `drafts` column with boolean flags (`is_most_popular`, `is_highest_value`, `is_most_drafted_3x`).
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

**Filter 1 — Slate Classification**
- Tiny (1-3 games): heavy team-stack, 1-2 pitchers
- Pitcher Day (4+ quality SP matchups): 4-5 pitchers
- Hitter Day (5+ games with O/U ≥ 9.0): 4-5 hitters
- Standard (10+ games, mixed): 2-3 pitchers + 2-3 hitters
- Classify BEFORE looking at any individual player. Constants in `app/core/constants.py`.

**Filter 2 — Environmental Advantage** (pre-game data only)
- Pitchers: weak opponent (OPS < .700), high K/9 (≥ 8.0), pitcher-friendly park, home field
- Batters: high Vegas total (O/U ≥ 8.5), weak opposing starter (ERA ≥ 4.5), platoon advantage, batting 1-4, hitter-friendly park
- env_score > 0.5 = passes. Stored on SlatePlayer. SlateGame holds the raw data (vegas_total, home/away_starter_era, etc.)
- If a field is NULL (data not yet available), scoring defaults to neutral — not fabricated

**Filter 3 — Ownership Leverage**
- FADE = crowd has found this player. 25% EV penalty (Moonshot: 40%).
- TARGET = crowd is ignoring this player. 15% EV bonus (Moonshot: 30%).
- Ghost players (< 100 drafts) who pass environmental filters are the highest-EV pool.
- Historical: most-drafted players chronically underperform. The crowd chases names.

**Filter 4 — Boost Optimization**
- Boost is a multiplier on an unknown outcome — it amplifies downside equally.
- card_boost ≥ 1.0 with env_score < 0.5 → **graduated** EV penalty (V2.2: 0% at env=0.5, up to 40% at env=0.0)
- Mega-ghost-boost players (3.0x, <50 drafts) get an EV floor to prevent data-scarcity from destroying their ranking
- Never assign a boost without environmental support.

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
The single most consistent edge: players with < 100 drafts who pass environmental filters. Historical examples: Miguel Vargas (1 draft, RS 6.2), Colson Montgomery (5 drafts, RS 6.3), Oneil Cruz (2 drafts, RS 5.7). The crowd chases Ohtani/Judge/Soto regardless of conditions — those three are chronically over-drafted.

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
