# Baseball DFS Predictor - AI Assistant Guide

## CRITICAL: This Is NOT Traditional DFS

This is **Real Sports DFS**. There is no salary cap. Players are drafted into 5 fixed slots with multipliers (2.0, 1.8, 1.6, 1.4, 1.2). Each player card has a **card_boost** (0 to +3.0x).

### The Core Formula (single source of truth: `app/core/utils.py`)

```
total_value = real_score × (2 + card_boost)
```

- `BASE_MULTIPLIER = 2.0` is defined in `app/core/utils.py`
- `compute_total_value()` and `compute_expected_total_value()` are the ONLY places this formula should be computed
- Slot multipliers are separate and defined in `app/core/constants.py`
- Lineup score = Σ real_score × (slot_mult + card_boost)

**Never hardcode `(2 + card_boost)` or `(2.0 + card_boost)` anywhere.** Always import from `app/core/utils.py`.

## Architecture Overview

### Three-Stage Pipeline
1. **Collect** (`app/services/data_collection.py`) — Fetch MLB schedule + player stats
2. **Score** (`app/services/scoring_engine.py`) — Rate players 0-100 via trait profiles
3. **Optimize** (`app/services/draft_optimizer.py`) — Assign cards to slots via rearrangement inequality

### Philosophy
Rule-based scoring engine (NOT ML) with a feedback loop. The goal is to **win drafts**, not predict Real Score. RS is opaque — we estimate it via player profiling and optimize around card boosts.

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
| `SlateGame` | slate_games | home_team, away_team, scores |
| `SlatePlayer` | slate_players | card_boost, real_score, total_value, leaderboard flags |
| `PlayerScore` | player_scores | total_score (0-100), estimated RS range |
| `ScoreBreakdown` | score_breakdowns | Per-trait scores |
| `DraftLineup` | draft_lineups | source, expected/actual values |
| `DraftSlot` | draft_slots | slot_index, slot_mult, card_boost |
| `CalibrationResult` | calibration_results | MAE, correlation, hit_rate |
| `WeightHistory` | weight_history | weights_json, effective_date |

## Scoring Engine (`app/services/scoring_engine.py`)

**Pitcher traits** (5 traits, 0-100): ace_status(25), k_rate(25), matchup_quality(20), recent_form(15), era_whip(15)

**Batter traits** (7 traits, 0-100): power_profile(25), matchup_quality(20), lineup_position(15), recent_form(15), ballpark_factor(10), hot_streak(10), speed_component(5)

Weights are configurable via the calibration API (`GET/PUT /api/calibration/weights`).

### Score-to-RS Mapping
- 80-100 → RS 4.0-6.0
- 60-79 → RS 2.5-4.0
- 40-59 → RS 1.5-2.5
- 20-39 → RS 0.5-1.5
- 0-19 → RS -0.5-0.5

## Shared Utilities (`app/core/utils.py`)

All shared formulas and lookups live here. **Always use these instead of reimplementing:**

- `compute_total_value(real_score, card_boost)` — The core formula
- `compute_expected_total_value(estimated_rs, card_boost)` — Rounded version for display
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

**Optimizer integration:** FADE players get 25% EV penalty, TARGET players get 15% EV bonus. Constants in `draft_optimizer.py:POPULARITY_ADJUSTMENTS`. Boost math still dominates — a FADE with +3.0x boost still beats a TARGET with no boost.

**Key distinction:** "Trending" ≠ "popular." A breakout rookie trending upward (TARGET) is different from a slumping star trending on ESPN (FADE). The aggregator distinguishes by cross-referencing attention volume against performance score.

## API Structure (7 routers under `/api/`)

| Router | Prefix | Purpose |
|---|---|---|
| players | `/api/players` | Player CRUD + search |
| slates | `/api/slates` | Slate management + draft cards + results |
| scoring | `/api/score` | On-demand scoring + rankings |
| draft | `/api/draft` | Lineup optimization + evaluation (popularity-aware) |
| calibration | `/api/calibration` | Feedback loop + weight tuning |
| pipeline | `/api/pipeline` | Orchestrated fetch → score → rank |
| popularity | `/api/popularity` | Player/slate popularity analysis |

## Core Rules & Business Logic

1. **Sport-Specific:** This is MLB only. Do NOT add NBA/NFL/etc. logic.
2. **total_value is absolute:** Always `real_score * (2 + card_boost)`. Never null.
3. **Enrichment:** Real Sports data does NOT provide Team or Position. The seed script and AI must append standard 3-letter MLB team abbreviations and positions.
4. **Volume:** Ownership volume uses `drafts` column with boolean flags (`is_most_popular`, `is_highest_value`, `is_most_drafted_3x`).
5. **DRY:** The total_value formula, player lookups, score queries, and game log sorting are centralized in `app/core/utils.py`.

## Strategy (Key Insights from Historical Analysis)

- **Winning formula**: All 5 RS ≥ 1.0 with 2+ RS ≥ 3.0
- **Anti-popularity edge**: Low-ownership players consistently outperform. Popularity is estimated via web signals (social, news, DFS ownership, search), NOT historical draft counts
- **Card boost leverage**: +3.0x boosted player with RS 2.4 = unboosted ace with RS 6.0
- **Pitcher profile**: Aces with high K rates in favorable matchups
- **Batter profile**: Power hitters batting 2-4 in hitter-friendly parks

## Deployment

- **Dockerfile** + **Procfile** included for Railway
- Environment vars use `DFS_` prefix (see `.env.example`)
- SQLite by default, swap `DFS_DATABASE_URL` for Postgres in production
- Database seeds automatically on startup via FastAPI lifespan
