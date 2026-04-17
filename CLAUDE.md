# Baseball DFS Predictor - AI Assistant Guide

## General Engineering Principles

These four principles (adapted from Karpathy's observations on common LLM coding pitfalls) apply to every change in this repo. They bias toward caution over speed — use judgment on trivial tasks.

### 1. Think Before Coding
**Don't assume. Don't hide confusion. Surface tradeoffs.**
- State assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First
**Minimum code that solves the problem. Nothing speculative.**
- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

The test: "Would a senior engineer call this overcomplicated?" If yes, simplify.

### 3. Surgical Changes
**Touch only what you must. Clean up only your own mess.**
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.
- Remove imports/variables/functions that YOUR changes orphaned. Don't remove pre-existing dead code unless asked.

The test: every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution
**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

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
- Silently swallow pipeline exceptions and serve old data
- Guess MLB IDs when no exact team match exists
- Substitute `card_boost` as a scoring input (it's during-draft only)

The pipeline either works with real data or it fails loudly. Fallbacks mask bugs, corrupt optimization with wrong data, and violate the "Filter, Not Forecast" philosophy — you cannot filter on yesterday's environment.

## Vegas Lines: Required, Never Optional

**Critical Requirement:** Vegas lines (moneyline + over/under totals) are **mandatory inputs** to the T-65 pipeline. The Odds API (`DFS_ODDS_API_KEY` environment variable) must be configured and operational.

**Behavior:**
- `DFS_ODDS_API_KEY` **must be set** at startup. If missing, the app logs a critical warning at initialization.
- When the T-65 pipeline runs (`app/services/pipeline.py:500`), `enrich_slate_game_vegas_lines()` **raises `RuntimeError`** if:
  - The API key is unset
  - The API request fails (network error, timeout)
  - The API response indicates an error (401 invalid key, 422 quota exhausted, etc.)
- **No fallback to NULL moneylines.** The entire T-65 pipeline crashes with a clear error.
- Users see HTTP 503 "T-65 lineup not available — Vegas API failed" when they try to fetch picks.

**Why?** Vegas lines feed directly into pitcher and batter environmental scoring (Filter 2):
- **Pitcher env (Factor 5):** Moneyline determines win-bonus probability (heavy favorite -250+ gets full credit).
- **Batter env (Group A, A1/A3):** Vegas O/U (over/under) and moneyline determine run-scoring environment.

Missing Vegas data corrupts the EV formula and produces suboptimal lineups. The system cannot proceed without it. If The Odds API fails, operations must investigate and restore it — there is no graceful degradation.

**Configuration:** Set `DFS_ODDS_API_KEY` to your The Odds API key (free tier: 500 requests/month, sufficient for one pipeline run per day).

## ABSOLUTE RULE: Historical Data Is Reference Only

**Historical stats from CSV/DB must NEVER be used as a direct input feature, normalization anchor, or baseline weight in the live daily pipeline.**

This means:
- `total_value`, `card_boost`, `drafts`, and leaderboard flags from `historical_players.csv` are **never** EV inputs — they're retrospective labels only.
- Past slate real scores and total values cannot feed forward into prediction or scoring.
- If a scoring baseline is needed, derive it from archetypal expectations (league-average defaults in `constants.py`) or conditional variables (pre-game conditions), not past performance.

**What IS permitted:**
- `PlayerStats` (ERA, WHIP, K/9, OPS, etc.) fetched from the live MLB Stats API — these are factual season aggregates.
- `PlayerGameLog` records for recent form — populated by `fetch_player_season_stats()` from the live MLB API. Historical CSV game logs (`hv_player_game_stats.csv`) are a supplementary seed only; the live API is authoritative.
- `historical_players.csv` for building the initial `Player` table (name, team, position, MLB ID) — identifying data, not predictive inputs.

**Why?** Using historical RS or leaderboard outcomes as predictive inputs creates data leakage — you'd be learning from outcomes that weren't knowable before the draft. The condition matrix in earlier versions (`RS_CONDITION_MATRIX`) was removed in V9.0 for exactly this reason.

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

### T-65 Sniper Architecture (Event-Driven Timing)

**The Core Rule: No pipeline work happens outside T-65. Zero API calls during active slates.**

The app uses an event-driven timing model that triggers the ONLY full pipeline run at exactly T-65 (65 minutes before first pitch). This ensures picks are locked and unchanged throughout the 60-minute user draft window.

**Four Phases Per Slate:**

1. **Initialization** (Morning until T-65): 
   - Startup completes cache warm-load (from Redis or DB if available)
   - `/api/filter-strategy/optimize` returns HTTP 425 "come back later"
   - `startup_done_event` signals the T-65 monitor that initialization is complete
   - **Zero API calls. Zero MLB data fetches. Zero optimization runs.**

2. **Before Lock** (Until T-65):
   - Slate monitor sleeps until T-65
   - Monitor publishes first-pitch time so `/status` can show countdown
   - `/api/filter-strategy/optimize` still returns HTTP 425 (not yet unlocked)

3. **T-65 Final Run** (Exactly at T-65):
   - Monitor wakes up and runs the FULL pipeline:
     - Fetch fresh MLB schedule (handles weather delays via retry loop)
     - Populate SlatePlayer rosters from MLB API boxscores
     - Fetch season stats for all players
     - Enrich game environment (Vegas lines, series context, bullpen ERA)
     - Score all players (0-100 trait profiles)
     - Run dual-filter strategy (Starting 5 + Moonshot)
   - **No fallbacks.** If any stage fails, the monitor crashes so `/optimize` returns HTTP 503.
   - Freeze cache with `lineup_cache.freeze()` — picks are now immutable

4. **T-60 Unlock & Post-Lock Monitoring** (After T-65):
   - At T-60 (60 minutes before first pitch), picks unlock for viewing
   - `/api/filter-strategy/optimize` serves frozen picks (zero computation per request)
   - Lightweight 60-second loop monitors game completion
   - On all-final, clear cache and pre-warm tomorrow's pipeline

**Timing Gates (Prevent Mid-Slate Interference):**

- **Manual pipeline endpoints** (`/api/pipeline/fetch`, `/api/pipeline/score`, `/api/pipeline/run`, `/api/pipeline/filter-strategy`) are locked during active slates. If today's slate has unfinished games, these endpoints return HTTP 423 "Locked — pipeline running via T-65 monitor". They only accept calls after all games finish.
- **The `/optimize` endpoint** never calls the pipeline. It serves cache only.
- **Start-up timing guard** (main.py): On app restart during a live slate (after T-65), restore frozen picks from SQLite/Redis instead of purging and regenerating. Prevents mid-game lineup changes due to dyno restarts.

**Why This Architecture Matters:**

1. **Pick Quality**: All candidate fetches, scoring, and optimization happen in one synchronized run with live data. No stale data. No partial updates.
2. **User Predictability**: Picks are locked 60 minutes before first pitch. Users know exactly when to draft.
3. **No Generational Drift**: No risk of serving lineups built from different versions of the schedule (e.g., lineup A built at 6:00 PM from 8 games, lineup B built at 6:15 PM from 7 games after a cancellation).
4. **Testability**: Manual endpoints (`/api/pipeline/*`) exist for post-slate analysis and testing, but are gated and only work after all games finish.

**Key Functions:**

- `app.services.slate_monitor.targeted_slate_monitor()` — Main T-65 event loop
- `app.services.slate_monitor._get_first_pitch_utc()` — Parse game times, compute lock time
- `app.services.slate_monitor._sleep_until()` — Chunked async sleep for responsive cancellation
- `app.services.lineup_cache.freeze()` — Freeze picks after T-65 run
- `app.core.utils.is_pipeline_callable_now()` — Gate manual pipeline endpoints

## Data Files (`/data/`)

Current coverage (as of 2026-04-14): **21 consecutive dates, 2026-03-25 → 2026-04-14**. All four files stay in lockstep — every date present in one is present in all four.

| File | Format | Current size | Purpose |
|---|---|---|---|
| `historical_players.csv` | CSV | 677 rows / 19 dates | Master player ledger (1 row per player/day). Contains total_value, card_boost, drafts, leaderboard flags. **Null `real_score` / `total_value` indicates a DNP / scratch — there is no separate flag.** Avg ~35 rows/date (range 22–56). |
| `historical_winning_drafts.csv` | CSV | 655 rows / 19 dates | Top-ranked lineups per date (5 rows per lineup = one per slot). Collection depth varies by date (4–12 ranks observed); the collector aspires to rank-20 but does not always reach it. |
| `historical_slate_results.json` | JSON array | 19 entries | Per-date MLB slate outcome envelope: `date`, `game_count`, `games[]`, `season_stage`, `source`, `saved_at`, `notes`. One object per slate day. |
| `hv_player_game_stats.csv` | CSV | 290 rows / 19 dates | Actual box score stats for every Highest-Value player appearance to date (grows each slate). Batting columns (ab, r, h, hr, rbi, bb, so) and pitching columns (ip, er, k_pitching, decision) coexist in one table — blanks indicate the column does not apply to that player. |

## Env Scoring Calibration

The env scoring thresholds in `app/core/constants.py` (BATTER_ENV_VEGAS_FLOOR, ERA floors/ceilings, etc.) are set by reasoning, not automation. To validate and adjust them from real outcome data:

**Step 1 — Capture conditions after each slate** (alongside the manual player ingest):
```bash
python scripts/export_slate_conditions.py   # exports today's SlateGame env data
```
This appends one row per game to `data/historical_conditions.csv`. Idempotent — safe to re-run.

**Step 2 — Run calibration analysis** (after accumulating 10+ new dates, or when pick quality degrades):
```bash
python scripts/calibrate_env_scoring.py
```
Output shows RS and HV-rate distributions across each threshold bucket (below floor / mid / above ceiling). No code is modified.

**Step 3 — Edit constants directly.** Read the output, decide which thresholds are misaligned, and update `app/core/constants.py`. Add or remove env factors by editing `compute_batter_env_score()` / `compute_pitcher_env_score()` in `app/services/filter_strategy.py`. Historical data teaches which conditions are predictive — it does not update the model automatically.

`scripts/recalibrate_condition_matrix.py` is dead code (the matrix it calibrated was removed in V9.0).

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

**card_boost must NEVER appear in the scoring engine.** The scoring engine runs pre-game; card_boost is only revealed during/after the draft. `compute_total_value()` in `app/core/utils.py` is the only place that uses card_boost — for computing historical total_value from CSV data, not for prediction or scoring.

**League-average defaults** for missing stats are centralized in `app/core/constants.py` (`DEFAULT_PITCHER_ERA`, `DEFAULT_PITCHER_WHIP`, `DEFAULT_OPP_OPS`, `DEFAULT_OPP_K_PCT`). Never hardcode these values inline.

**Scaling thresholds** (K/9 floor/ceiling, ERA ranges, OPS ranges, etc.) are also centralized in `app/core/constants.py` (`SCORING_K9_FLOOR`, `SCORING_K9_CEILING`, etc.). The scoring engine uses `scale_score()` from `app/core/utils.py` for all linear scaling — never inline `max(0, min(...))`.

**Graduated env-score thresholds** for both pitcher and batter env functions are in `app/core/constants.py` (`PITCHER_ENV_OPS_FLOOR`, `BATTER_ENV_VEGAS_CEILING`, etc.). The service layer uses `_graduated_scale()` and `_graduated_scale_moneyline()` helpers in `app/services/filter_strategy.py`.

## Signal Isolation: ABSOLUTE RULE

**CRITICAL: `card_boost` and `drafts` must NEVER appear in EV calculations at any stage.**

These are **in-draft dynamic signals** revealed ONLY during/after the draft. They can never be predictive inputs.

Where they ARE permitted (display-only):
- `FilteredCandidate.card_boost` — stored for user display during draft
- `FilteredCandidate.drafts` — stored for user display (crowd ownership signals)
- `compute_total_value()` in `app/core/utils.py` — ONLY for historical record-keeping from CSV data, never for prediction
- Database fields on `SlatePlayer` — historical record-keeping

Where they are FORBIDDEN:
- `_compute_base_ev()` — no card_boost input (line 707-816 of filter_strategy.py) ✓
- `_compute_filter_ev()` — delegates to `_compute_base_ev(candidate)`, no popularity input ✓
- Scoring engine (`app/services/scoring_engine.py`) — absolutely no card_boost or drafts ✓
- Trait scoring — no dynamic signals ✓
- Environmental scoring — no platform data ✓

**Rationale:** Card boost is unknown before the draft. Drafts (platform ownership) are only visible during the draft. Using either for pre-game prediction would leak in-game information, corrupting the entire model with data unavailable at analysis time.

**Enforcement:** Code review must flag any mention of `card_boost` or `drafts` in optimization, scoring, or EV-related logic. These fields are read-only for display purposes.

## Disaster Recovery

See **DISASTER_RECOVERY.md** for the complete runbook covering seven failure scenarios:
1. Database unavailable
2. Odds API key invalid/quota exhausted
3. Redis unavailable
4. MLB API down
5. T-65 monitor hangs (weather delay)
6. App crashes during pipeline
7. Redis cache corrupted

All scenarios follow the **FAIL LOUDLY, NEVER FALLBACK** rule. Users see HTTP 503 with clear error messages; operations must restore the system.

## Shared Utilities (`app/core/utils.py`)

All shared formulas and lookups live here. **Always use these instead of reimplementing:**

- `compute_total_value(real_score, card_boost)` — The core formula (historical data only — NOT for scoring/prediction)
- `find_player_by_name(db, name, team)` — Accent-insensitive player lookup
- `get_latest_player_score(db, slate_player_id)` — Most recent PlayerScore
- `get_recent_games(game_logs, n)` — N most recent games sorted by date
- `scale_score(value, floor, ceiling, max_pts)` — Linear scaling helper (use this, never inline `max(0, min(...))`)

## Shared Constants (`app/core/constants.py`)

All magic numbers, thresholds, and league-average defaults are centralized here. **Never hardcode these inline:**

- **League-average defaults:** `DEFAULT_OPP_OPS`, `DEFAULT_OPP_K_PCT`, `DEFAULT_PITCHER_ERA`, `DEFAULT_PITCHER_WHIP`
- **Scoring thresholds:** `SCORING_K9_FLOOR/CEILING`, `SCORING_ERA_CEILING/RANGE`, `SCORING_PITCHER_OPS_CEILING/RANGE`, etc.
- **Pitcher env thresholds:** `PITCHER_ENV_OPS_FLOOR/CEILING`, `PITCHER_ENV_K9_FLOOR/CEILING`, `PITCHER_ENV_ML_FLOOR/CEILING`, etc.
- **Batter env thresholds:** `BATTER_ENV_VEGAS_FLOOR/CEILING`, `BATTER_ENV_ERA_FLOOR/CEILING`, `BATTER_ENV_WIND_OUT_DIRECTIONS`, etc.
- **Unknown-data neutral:** `UNKNOWN_SCORE_RATIO = 0.5` — used when trait data is missing

## Graduated Scaling Helpers (`app/services/filter_strategy.py`)

The env-score functions use shared helpers instead of duplicated inline patterns:

- `_graduated_scale(value, floor, ceiling)` → 0.0–1.0 (works for ascending and descending ranges)
- `_graduated_scale_moneyline(moneyline, ml_floor, ml_ceiling)` → 0.0–1.0 (negative-number-aware)

## Popularity Signal Aggregator (`app/services/popularity.py`)

Web-scraping signal aggregator that estimates crowd media attention. This is NOT rule-based — it's dynamic, fetching real-time public signals from sources knowable before the draft begins.

**Signal sources (weighted) — pre-game public signals only:**
- Social trending (45%): Google Trends autocomplete + daily trends
- Sports news (25%): ESPN and MLB.com RSS feeds
- Search volume (30%): Google autocomplete context terms

**Intentionally excluded:** DFS platform ownership data (RotoGrinders, NumberFire). Platform ownership is only visible during the draft — using it would violate the pre-game signals constraint.

**Classification logic:**
- High media attention + any performance level → **FADE** (crowd is already here)
- High performance + low attention → **TARGET** (under the radar)
- Low attention + mid performance → **TARGET** (value pick)
- Otherwise → **NEUTRAL**

**V8.0 Optimizer integration:** The popularity classification is the **primary EV signal** (3.0× swing, 0.50–1.50). It drives the core FADE/TARGET decision — the crowd is structurally wrong about batters (3.6× RS differential). Environmental and trait signals differentiate within popularity tiers. DFS platform ownership was never pre-game knowable and is fully excluded.

**Sharp signal (underground):** A 4th source scraped from Reddit (r/fantasybaseball, r/baseball), FanGraphs community blogs, and Prospects Live. Used exclusively by the Moonshot lineup. `sharp_score` is 0-100, separate from the composite score.

## Dual-Lineup Optimizer (`app/services/filter_strategy.py`)

**Strategy Version: V9.0 "Popularity as Gate — Env/Trait Drive EV"** — The optimizer is built exclusively from information available before any draft begins. Card boosts and platform draft counts are **not optimizer inputs**. FADE players (high pre-game media attention) are **excluded from the candidate pool** before EV computation begins. EV is driven purely by game conditions and player traits.

### V9.0 Core Architecture (Popularity Gate + Env/Trait EV)

**Popularity gate (applied first, before any EV):**
```
candidates = [c for c in candidates if c.popularity != FADE]
```
FADE players never reach EV scoring. TARGET and NEUTRAL players pass the gate and are scored identically — no popularity bonus or penalty in EV.

**The EV formula:**
```
base_ev = env_factor × trait_factor × context × 100
```

| Signal | Source | Range | Role |
|---|---|---|---|
| env_factor | Pre-game conditions (Vegas O/U, ERA, bullpen ERA, park, weather, platoon, batting order, moneyline, series context) | 0.70–1.30 | **Primary** — 1.86× swing |
| trait_factor | Scoring engine (K/9, ISO, barrel%, SB pace, ERA, WHIP, recent form, 0-100) | 0.85–1.15 | **Secondary** — 1.35× swing |
| context | stack_bonus × dnp_adj | varies | Situational modifiers |

**Moonshot differentiation** — same candidate pool as Starting 5, but a different formula:
```
moonshot_ev = base_ev × sharp_bonus × explosive_bonus
```
- `sharp_bonus`: up to +35% from underground analyst buzz (Reddit, FanGraphs, Prospects Live)
- `explosive_bonus`: up to +20% from power_profile (batters) or k_rate (pitchers)
- `MOONSHOT_SAME_TEAM_PENALTY = 0.85` — soft push toward different team combinations
- Player overlap with Starting 5 is allowed; formula divergence naturally reorders picks

### V9.0 Lineup Composition: 1P + 4B

Exactly 1 pitcher + 4 batters. The pitcher anchors Slot 1 (2.0×). The pre-game EV formula determines WHICH pitcher and WHICH 4 batters.

Structural constraints:
- `REQUIRED_PITCHERS_IN_LINEUP = 1` (exactly 1 pitcher per lineup)
- `MAX_PLAYERS_PER_TEAM = 1` (team diversification)
- `MAX_PLAYERS_PER_GAME = 1` (game diversification)
- Pitcher's `game_id` is blocked for all batter picks (no negative correlation)
- Slot 1 = pitcher anchor, Slots 2-5 = batters by filter_ev descending

The active optimizer produces **two lineups** from the same FADE-excluded pool via `run_dual_filter_strategy`.

### EV Formula (V9.0 — env/trait-only, single source: `_compute_base_ev()`)
```
filter_ev = env_factor                           # PRIMARY: 0.70–1.30 (1.86× swing)
  × trait_factor                                 # SECONDARY: 0.85–1.15 (1.35× swing)
  × stack_bonus (1.20 if blowout game, else 1.0)
  × dnp_adj (unknown=0.85, confirmed_bad=0.70)
  × 100
```

**What each term uses (pre-game data only):**
- `env_factor`: Vegas O/U, opposing starter ERA, bullpen ERA, platoon advantage, batting order (graduated 1-9), park factor, weather (wind/temp), moneyline, series context (wins leading/trailing), recent form (L10 wins).
- `trait_factor`: K/9, ISO, barrel%, SB pace, ERA, WHIP, recent form (L7 games)

card_boost and draft counts are **display-only** fields stored on `FilteredCandidate`. They are not inputs to the EV formula.

Post-EV composition (applied in `_enforce_composition`):
- **Pitcher anchor (Phase 1):** highest-EV pitcher selected, pinned to Slot 1. Its `game_id` is blocked for all batter picks.
- **Batter fill (Phase 2):** top-4 batters by filter_ev, honouring MAX_PLAYERS_PER_TEAM=1 and MAX_PLAYERS_PER_GAME=1.

### V8.0 Changes (April 14 — Popularity-First Signal Hierarchy & Env Refinements)

**Design change:** The signal hierarchy is inverted based on 20-date empirical analysis. The crowd-avoidance signal (FADE/TARGET/NEUTRAL) is now the **primary** EV driver with a 3.0× swing, replacing environment (demoted to secondary, 1.86× swing) and trait (demoted to tertiary, 1.35× swing). This matches the observed 3.6× RS differential between TARGET and FADE batters.

**Six changes:**

1. **Signal hierarchy inversion** (`app/core/constants.py`) — `POP_MODIFIER` range expanded to 0.50–1.50 (PRIMARY). `ENV_MODIFIER` compressed to 0.70–1.30 (SECONDARY). `TRAIT_MODIFIER` compressed to 0.85–1.15 (TERTIARY). The RS_CONDITION_MATRIX raw factor (0.275 for FADE batters, 1.00 for TARGET) now maps to the full 3.0× swing instead of being compressed to ±15%.

2. **Pitcher moneyline added** (`app/services/filter_strategy.py`) — `compute_pitcher_env_score()` now accepts `team_moneyline`. Win bonus probability is a major pitcher RS component; heavy favorites (-250+) get full credit. Graduated from -110 (0) to -250 (1.0). `max_score` raised from 5.5 to 6.0.

3. **Batter env correlated-signal cap** (`app/services/filter_strategy.py`) — `compute_batter_env_score()` restructured into three signal groups. Group A (run environment: O/U, opposing ERA, moneyline, bullpen) **capped at 2.0** to prevent 4 correlated signals from inflating env score. Group B (player situation: platoon, batting order) up to 2.0. Group C (venue: park + weather) up to 1.0. `max_score` reduced from 7.5 to 5.5.

4. **Batting order graduated** — Hard top-5 gate replaced with graduated scale: order 1-3 → 1.0, 4-5 → 0.75, 6-7 → 0.50, 8-9 → 0.25. **Unknown batting order gets 0.40 baseline** (neutral assumption) instead of 0, removing the structural penalty on ghost players whose orders are unpublished pre-game.

5. **All thresholds graduated** — Binary thresholds replaced with linear interpolation across both pitcher and batter env functions. Examples: K/9 from 6.0 (0) to 10.0 (1.0); opposing OPS from 0.780 (0) to 0.650 (1.0); park factor from 1.05 (0) to 0.90 (1.0). Eliminates false-precision cliffs on early-season sample sizes.

6. **Strategy documentation updated** — Filter 2 (now "Popularity / Crowd-Avoidance") formalized as the primary filter with empirical basis. Filter 3 (now "Environmental Advantage") demoted to secondary with correlated-signal grouping documented.

**New constants:**
- `POP_MODIFIER_FLOOR = 0.50`, `POP_MODIFIER_CEILING = 1.50` (was 0.85/1.15)
- `ENV_MODIFIER_FLOOR = 0.70`, `ENV_MODIFIER_CEILING = 1.30` (was 0.50/1.50)
- `TRAIT_MODIFIER_FLOOR = 0.85`, `TRAIT_MODIFIER_CEILING = 1.15` (was 0.70/1.30)

### V8.1 Changes (April 15 — Series Context, Bullpen ERA, Vegas Lines, Cache Restart Guard)

**Six production fixes addressing the April 14 post-mortem (0/4 batters, Buxton missed).**

**Fix 1 — Cache restart guard** (`app/main.py`, `app/services/lineup_cache.py`)
- Root cause: `lineup_cache.purge()` was called unconditionally on every app start. A Railway dyno restart after T-65 wiped the frozen picks and regenerated from a smaller pool (started/final games excluded), producing different picks mid-slate.
- Fix: On startup, check if today's slate is active AND T-65 has already passed. If so, call `lineup_cache.restore_and_refreeze(first_pitch_utc)` — loads from SQLite/Redis and re-freezes without regenerating. Only purge on normal (pre-T-65) restarts.
- `slate_monitor.py` Phase 3 guarded with `if lineup_cache.is_frozen:` — skips the final pipeline run if picks were already restored on startup.

**Fix 2 — Bullpen ERA** (`app/core/mlb_api.py`, `app/services/data_collection.py`)
- Added `get_team_pitching_stats(team_id, season)`. `enrich_slate_game_team_stats()` now fetches hitting + pitching in parallel, populating `home/away_bullpen_era` on `SlateGame`.
- Feeds Group A A4 (`opp_bullpen_era`) in `compute_batter_env_score()` — was always NULL before.

**Fix 3 — Series/H2H context** (`app/models/slate.py`, `app/services/data_collection.py`, `app/services/filter_strategy.py`, `app/core/constants.py`)
- Added 4 nullable columns to `SlateGame`: `series_home_wins`, `series_away_wins`, `home_team_l10_wins`, `away_team_l10_wins`.
- `enrich_slate_game_series_context()`: fetches last 14 days of each team's schedule, computes current-series wins and last-10-game wins.
- **Group D env scoring** (±0.8 additive): series leading ≥2 → +0.6; trailing ≥2 → −0.6; hot L10 ≥7 → +0.2; cold L10 ≤3 → −0.2. `BATTER_ENV_MAX_SCORE` raised to 6.3 (then reduced to 5.8 after debut bonus removed).
- **Momentum gate** (removed in V9.0): previously capped `pop_factor` at NEUTRAL for cold/trailing teams. Removed when pop_factor was removed from EV. Series context still contributes to env Group D scoring.

**Fix 4 — Vegas lines** (`app/core/odds_api.py`, `app/config.py`, `app/services/data_collection.py`, `app/services/pipeline.py`)
- New `app/core/odds_api.py` client. `DFS_ODDS_API_KEY` env var; omitting it skips enrichment with a loud warning (env scoring treats NULL lines as unknown/neutral — existing behavior).
- `enrich_slate_game_vegas_lines()` populates `vegas_total`, `home_moneyline`, `away_moneyline`. Non-fatal in `run_full_pipeline()`.

**Fix 5 — Condition matrix** (`app/services/condition_classifier.py`)
- Version bumped to `"6.1"`. `RS_CONDITION_OBSERVATIONS` updated with April 14 outcomes.

**Fix 6 — Quality pass** (multiple files)
- `NON_PLAYING_GAME_STATUSES` extracted to `constants.py`; 3 inline duplicates removed.
- All inline imports inside hot functions moved to module-level in `filter_strategy.py`.
- Module-level loggers added to `data_collection.py` and `pipeline.py`.
- `_extract_record()` returns `(None, None, None)` on empty data — prevents momentum gate false-positive on partial API failure.

**New constants (`app/core/constants.py`):**
- `NON_PLAYING_GAME_STATUSES`, `SERIES_LEADING_BONUS`, `SERIES_TRAILING_PENALTY`, `TEAM_HOT_L10_THRESHOLD`, `TEAM_COLD_L10_THRESHOLD`, `TEAM_HOT_L10_BONUS`, `TEAM_COLD_L10_PENALTY`, `BATTER_ENV_MAX_SCORE = 5.8`, `MOMENTUM_GATE_SERIES_DEFICIT = 2`, `MOMENTUM_GATE_L10_CEILING = 3`

---

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
- (The V3.0 gradient-booster `ml_model.py` was removed on 2026-04-15: it accepted `drafts` and `card_boost` as predictive inputs, violating the Prime Directive that post-slate variables must never feed pre-game prediction. It was dead code — no importers, no trained artifact on disk. The Bayesian-smoothed matrix remains the sole signal for previously-dead-capital conditions.)

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

### Lineup Construction (V8.0 — Pitcher-Anchor + Popularity-First EV)
Every lineup is **exactly 1 SP + 4 batters**. The count is fixed, not data-driven.

1. **Pitcher anchor (Phase 1)**: pick the highest-EV pitcher (by pre-game EV: K/9 + opponent K% + OPS + park + home + moneyline). Pin to Slot 1 (`PITCHER_ANCHOR_SLOT = 1`, 2.0× multiplier). Block its `game_id`.
2. **Fill 4 batter slots (Phase 2)**: fill from batters sorted by filter_ev descending, honouring `MAX_PLAYERS_PER_TEAM = 1`, `MAX_PLAYERS_PER_GAME = 1`, and the blocked pitcher-game.
3. **No-fallback rule**: if the pool contains no pitcher, raise `ValueError` — do not substitute.

### Lineup Validation (V8.0)
- **Max 1 player per team** per individual lineup — anchor pitcher exempt
- **Max 1 player per game** per individual lineup — anchor pitcher is the seed, 4 batters come from 4 different non-anchor games
- **Exactly 1 pitcher** (`REQUIRED_PITCHERS_IN_LINEUP = 1`) — enforced by final assertion in `_validate_lineup_structure`

### Slate Classification (informational only — does NOT force composition)
- Classification exists for blowout detection and display only
- **No slate type forces pitcher/hitter counts.**

**Moonshot** — V9.0: draws from the same FADE-excluded pool as Starting 5. Player overlap is allowed.
- Same structural shape: **1 SP anchor in Slot 1 + 4 batters in Slots 2–5**
- Moonshot's anchor game_id is blocked for Moonshot batters independently
- **No contrarian multipliers** — FADE players excluded at the gate, not penalised in EV
- Sharp signal bonus: up to +35% EV from underground analyst buzz (Reddit, FanGraphs, Prospects Live)
- Explosive bonus: up to +20% EV from power_profile (batters) or k_rate (pitchers)
- `MOONSHOT_SAME_TEAM_PENALTY = 0.85` — soft push toward different team combinations vs S5
- Natural formula divergence (sharp × explosive re-ranks candidates differently from env × trait alone)

**Key functions (filter_strategy.py):**
- `run_filter_strategy()` — Starting 5 (V9.0: env/trait EV, pitcher-anchor)
- `run_dual_filter_strategy()` — One call, two lineups from same FADE-excluded pool
- `_exclude_fade_players()` — Hard gate: removes FADE candidates before EV, raises ValueError if no pitchers remain
- `_compute_base_ev()` — Shared formula: env × trait × context × 100
- `_compute_filter_ev()` — Starting 5 EV (delegates to `_compute_base_ev`)
- `_compute_moonshot_filter_ev()` — Moonshot EV (delegates to `_compute_base_ev` + sharp/explosive bonuses)
- `_compute_dnp_adjustment()` — Bifurcated DNP risk (unknown=0.85, confirmed_bad=0.70)
- `_enforce_composition()` — Phase 1 picks highest-EV pitcher; Phase 2 fills 4 batters by filter_ev with team/game caps. Raises `ValueError` if pool has no pitcher.
- `_validate_lineup_structure()` — Enforces team/game caps; anchor pitcher protected; final pitcher-count assertion.
- `_smart_slot_assignment()` — Pitcher → Slot 1; batters → Slots 2-5 by filter_ev descending.

**Key functions (condition_classifier.py):**
- `get_rs_condition_factor()` — (position_type, popularity_class) → RS factor (used as tertiary pop signal)

**Key functions (routers/filter_strategy.py):**
- `_resolve_candidates()` — Builds candidate pool from DB, scores env + traits, fetches web-scraped popularity (no platform ownership sources)

**Dead code:** `app/services/draft_optimizer.py` — not wired to any router except `evaluate_lineup`. The filter_strategy path supersedes it entirely.

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
2. **No fallbacks ever.** See "ABSOLUTE RULE" section above. If the pipeline fails, raise an error — never silently serve stale data.
3. **total_value is absolute:** Always `real_score * (2 + card_boost)`. Never null. Computed only via `compute_total_value()` in `app/core/utils.py`.
4. **card_boost is during-draft only.** It must NEVER appear as an input to the scoring engine, EV formula, or any pre-game prediction. `card_boost` exists only in: (a) `compute_total_value()` for historical CSV data, (b) display-only fields on `FilteredCandidate`, (c) data models for storage. The scoring engine runs pre-game and cannot use card_boost.
5. **Enrichment:** Real Sports data does NOT provide Team or Position. The seed script and AI must append standard 3-letter MLB team abbreviations and positions.
6. **Volume:** Ownership volume uses `drafts` column with boolean flags (`is_most_popular`, `is_highest_value`, `is_most_drafted_3x`). Note: `is_most_drafted_3x` is retrospective in the DB — the optimizer recomputes it dynamically each run (top-5 most-drafted with boost ≥ 3.0) so the V2.3 trap penalty fires for live slates.
7. **DRY:** The total_value formula, player lookups, score queries, game log sorting, and linear scaling are centralized in `app/core/utils.py`. League-average defaults and all graduated-scaling thresholds are in `app/core/constants.py`. Never hardcode magic numbers inline.
8. **is_highest_value / is_most_popular flags are retrospective labels.** Never use them as inputs to prediction or optimization — that is a data leak. They reflect post-hoc outcomes only.
9. **No guessing MLB IDs.** If a player name search returns no exact team match, return `None` — never assign the first result as a fallback. Wrong MLB IDs corrupt all downstream stats.

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

**Filter 2 — Popularity / Crowd-Avoidance (PRIMARY EV SIGNAL — V8.0)**
- **This is the dominant filter.** The crowd is structurally wrong about batters.
- FADE = high media attention → **PRIMARY penalty (3.0× swing in V8.0)**. FADE batters average RS 0.98, HV rate 9.6%. Raw matrix factor 0.275 → scaled to 0.50× in EV.
- TARGET = low media attention → **PRIMARY bonus**. TARGET batters average RS 3.57, HV rate 73.6%. Raw matrix factor 1.00 → scaled to 1.50× in EV.
- NEUTRAL = moderate buzz → interpolated at 0.65 raw → ~1.0× in EV.
- Pitcher differential is smaller (1.4×): TARGET pitcher RS 4.36 vs FADE pitcher RS 3.09. Crowd is less wrong about pitchers because pitcher outcomes are one-player-dependent.
- Source: Google Trends, ESPN RSS, Reddit — **NOT** DFS platform ownership (during-draft only).
- draft counts and card boosts are NOT available pre-game and are NOT EV inputs.
- **Key principle: no amount of environmental advantage rescues a FADE batter from RS ~1.0.**

**Filter 3 — Environmental Advantage (SECONDARY — pre-game data only)**
- Pitchers: weak opponent OPS (graduated 0.780→0, 0.650→1.0), high opponent K% (graduated 0.20→0, 0.26→1.0), high K/9 (graduated 6.0→0, 10.0→1.0), pitcher-friendly park (graduated 1.05→0, 0.90→1.0), **moneyline favorite (graduated -110→0, -250→1.0 — Win bonus probability)**, home field (+0.5)
- Batters: correlated run-environment signals (O/U, opposing ERA, moneyline, bullpen ERA) are **grouped and capped at 2.0** to prevent redundancy inflation. Independent signals: platoon advantage, batting order (graduated 1-9 with neutral baseline for unknowns), park + weather.
- All thresholds are graduated (linear interpolation) — no hard cliffs on April sample sizes.
- env_score > 0.5 = passes. Stored on SlatePlayer. If a field is NULL, scoring defaults to neutral — not fabricated.

**Filter 4 — Individual Explosive Traits (TERTIARY)**
- Power upside: ISO ≥ .250, barrel% high, HR/PA ≥ 6% → elevated power_profile score
- Speed upside: SB pace ≥ 30/season → elevated speed_component score
- Pitcher K upside: K/9 ≥ 9.0 → elevated k_rate score
- These flow through the trait_factor (tertiary signal, 0.85–1.15) — they break ties within the same pop+env tier

**Filter 5 — Slot Sequencing (V8.0 pitcher-anchor)**
- **Slot 1 (2.0×) is always the anchor pitcher.** Pitcher is selected by highest pre-game EV.
- Slots 2–5 are batters, ordered by filter_ev descending. Batters from different games, each from a unique team.

### Fixed Composition (V8.0): 1 SP + 4 Batters, Always
Every lineup is 1 pitcher + 4 batters. The pitcher is the best-condition pre-game SP. The 4 batters are the highest-EV independent batters across the slate, drawn from different games.

## Deployment

- **Dockerfile** + **Procfile** included for Railway
- Environment vars use `DFS_` prefix (see `.env.example`)
- SQLite by default, swap `DFS_DATABASE_URL` for Postgres in production
- Database seeds automatically on startup via FastAPI lifespan
- Startup does **zero** pipeline work — the T-65 slate monitor is the sole pipeline trigger
- If the T-65 pipeline fails, the app returns HTTP 503 from `/api/filter-strategy/optimize` — this is correct behavior
