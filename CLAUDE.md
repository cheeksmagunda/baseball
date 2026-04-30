# Ben Oracle - AI Assistant Guide

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

## MLB Season Calendar (Basic Sport Knowledge)

**One MLB season is fully contained within a single calendar year.** There is no cross-year ambiguity — the 2026 season starts and ends in 2026.

Rough calendar (dates drift slightly year to year, but this is the shape):

| Phase | When | Activity |
|---|---|---|
| Spring Training | Mid-February → late March | Exhibition games in Arizona (Cactus League) and Florida (Grapefruit League). Not counted in season stats. |
| Regular Season | Late March → late September / early October | 162 games per team. This is what Ben Oracle optimizes for. |
| Postseason | October → early November | Wild Card, Division Series (LDS), League Championship (LCS), World Series. Single-elimination / short series — no DFS slates here. |
| Offseason | November → mid-February | No games. Free agency, trades, roster construction. |

**Operational implications:**
- `BO_CURRENT_SEASON` is the year of the regular season Ben Oracle is running against (e.g., `2026`). It does **not** auto-derive from `datetime.now().year` — it's explicitly set per deploy. Change it once a year when spring training ends, and leave it alone otherwise.
- During the offseason (November → February), the pipeline is idle. `BO_CURRENT_SEASON` still points at the just-completed season for reference/backfill purposes.
- Don't think about "next season" while the current season is live. One season at a time.

### MLB Data Sources & Concepts (quick reference)

- **MLB Stats API** (`https://statsapi.mlb.com/api/v1`) — primary data source. Used for schedule, boxscores, player season stats, team records. Free, no key required.
- **The Odds API** (`BO_ODDS_API_KEY`) — source for Vegas lines (moneyline, over/under totals). Mandatory per-slate input.
- **Season stats** — cumulative across the regular season, fetched via `get_player_stats(mlb_id, BO_CURRENT_SEASON)`.
- **Slate** — the set of games on a given day that users can draft from.
- **First pitch** — the earliest game's scheduled start. T-65 is 65 minutes before this.
- **Probable pitcher** — the announced starting pitcher for a team in an upcoming game. Available 1–3 days in advance.
- **Lineup card** — the batting order for a team in a specific game. Usually published 2–4 hours before first pitch; sometimes delayed on weather-threatened slates.
- **Position codes** — `P` (pitcher), `C` (catcher), `1B/2B/3B` (infield), `SS` (shortstop), `OF` (outfielder), `DH` (designated hitter).
- **Stat abbreviations** — `AB` (at-bats), `H` (hits), `HR` (home runs), `RBI` (runs batted in), `SB` (stolen bases), `OPS` (on-base plus slugging), `ISO` (isolated power), `ERA` (earned run average), `WHIP` (walks+hits per inning), `K/9` (strikeouts per nine innings), `IP` (innings pitched).
- **Home/away convention** — home team listed second in standard notation (`BOS @ NYY` means Boston at New York).

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

**Critical Requirement:** Vegas lines (moneyline + over/under totals) are **mandatory inputs** to the T-65 pipeline. The Odds API (`BO_ODDS_API_KEY` environment variable) must be configured and operational.

**Behavior:**
- `BO_ODDS_API_KEY` **must be set** at startup. If missing, the app logs a critical warning at initialization.
- When the T-65 pipeline runs (`app/services/pipeline.py:537`), `enrich_slate_game_vegas_lines()` **raises `RuntimeError`** if:
  - The API key is unset
  - The API request fails (network error, timeout)
  - The API response indicates an error (401 invalid key, 422 quota exhausted, etc.)
- **No fallback to NULL moneylines.** The entire T-65 pipeline crashes with a clear error.
- Users see HTTP 503 "T-65 lineup not available — Vegas API failed" when they try to fetch picks.

**Why?** Vegas lines feed directly into pitcher and batter environmental scoring (Filter 2):
- **Pitcher env (Factor 5):** Moneyline determines win-bonus probability (heavy favorite -250+ gets full credit).
- **Batter env (Group A, A1/A3):** Vegas O/U (over/under) and moneyline determine run-scoring environment.

Missing Vegas data corrupts the EV formula and produces suboptimal lineups. The system cannot proceed without it. If The Odds API fails, operations must investigate and restore it — there is no graceful degradation.

**Configuration:** Set `BO_ODDS_API_KEY` to your The Odds API key (free tier: 500 requests/month, sufficient for one pipeline run per day).

## ABSOLUTE RULE: Historical Data Is Reference Only

**Historical stats from CSV/DB must NEVER be used as a direct input feature, normalization anchor, or baseline weight in the live daily pipeline.**

This means:
- `total_value`, `card_boost`, `drafts`, and leaderboard flags from `historical_players.csv` are **never** EV inputs — they're retrospective outcome labels only.
- Past slate real scores and total values cannot feed forward into prediction or scoring.
- If a scoring baseline is needed, derive it from archetypal expectations (league-average defaults in `constants.py`) or conditional variables (pre-game conditions), not past performance.

**What IS permitted:**
- `PlayerStats` (ERA, WHIP, K/9, OPS, etc.) fetched from the live MLB Stats API — these are factual season aggregates.
- `PlayerGameLog` records for recent form — populated by `fetch_player_season_stats()` from the live MLB API. Historical CSV game logs (`hv_player_game_stats.csv`) are a supplementary seed only; the live API is authoritative.
- `historical_players.csv` for building the initial `Player` table (name, team, position, MLB ID) — identifying data, not predictive inputs.

**Why?** Using historical RS or leaderboard outcomes as predictive inputs creates data leakage — you'd be learning from outcomes that weren't knowable before the draft. The condition matrix in earlier versions (`RS_CONDITION_MATRIX`) was removed in V9.0 for exactly this reason.

**ABSOLUTE RULE: Do not create scripts that analyse historical outcome data.** Scripts that read `real_score`, `total_value`, `is_highest_value`, `is_most_popular`, `is_most_drafted_3x`, or `drafts` for any calibration, training, or threshold-recommendation purpose are forbidden — even if they only write to stdout. They create a feedback loop risk: analysis outputs inform constant edits, which moves the scoring model toward historical outcomes, which is exactly the data leakage the architecture is designed to prevent. Calibration is done manually by Claude reading the files directly.

### What historical data IS for: calibration

The historical files exist to answer one question: **do the live signals we score on actually correlate with real outcomes?**

The live pipeline consumes two categories of signal at T-65:
1. **Player performance signals** — season stats (ERA, K/9, OPS, recent form) from the MLB Stats API
2. **Game context signals** — Vegas lines, weather, bullpen ERA, series context, etc.

The historical data captures outcomes of those same conditions:
- `hv_player_game_stats.csv` — what the top players actually did (box scores). Ground truth on player performance.
- `historical_slate_results.json` (game objects, including env fields once populated) — what the game context actually looked like. Ground truth on game conditions.
- `historical_players.csv` — real_score and HV/MP/3X flags. Outcome labels to measure prediction quality against.

The calibration loop: join game context conditions (game objects in historical_slate_results.json, enriched by `export_slate_conditions.py`) with player outcomes (real_score, HV flags from historical_players.csv) on (date, team) → see how outcomes distribute across each scoring threshold → tune `app/core/constants.py`. `scripts/calibrate_env_scoring.py` automates this join and analysis.

## Architecture Overview

### Active Pipeline

The active (and only) optimization path is `filter_strategy`.

**Four-Stage Pipeline:**
1. **Collect** (`app/services/data_collection.py`) — Fetch MLB schedule + boxscores + player stats
2. **Score** (`app/services/scoring_engine.py`) — Rate players 0-100 via trait profiles
3. **Filter** (`app/services/filter_strategy.py`) — Score env (Vegas O/U, opp ERA/WHIP/K9, bullpen, park, weather, platoon, batting order, ML, series, L10, opp rest days)
4. **Optimize** (`app/routers/filter_strategy.py` → `run_filter_strategy`) — Produce a single lineup (1P+4B or 0P+5B chosen by total EV)

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
     - **Phase 1: RotoWire expected lineups** (best-effort) — populates `batting_order` from beat-reporter projections, sets `batting_order_source` to `"rotowire_confirmed"` or `"rotowire_expected"`. Fails gracefully (warns + continues) if RotoWire is unreachable; downstream DNP_UNKNOWN_PENALTY (V10.6: 0.93, was 0.85) absorbs missing data.
     - **Phase 2: MLB Stats API boxscore** (ground truth) — overrides any RotoWire value with the official lineup card when posted, sets source to `"official"`. Typically populates 30-60 min before first pitch (i.e., usually after T-65), so RotoWire carries the load at the lock moment.
     - Fetch season stats for all players
     - Enrich game environment (Vegas lines, series context, bullpen ERA)
     - Score all players (0-100 trait profiles)
     - Run filter strategy (single lineup, V11.0)
   - **No fallbacks.** If any stage fails (except Phase 1 RotoWire), the monitor crashes so `/optimize` returns HTTP 503.
   - Freeze cache with `lineup_cache.freeze()` — picks are now immutable

4. **Post-Lock Monitoring** (After T-65):
   - Picks are available immediately once the T-65 pipeline completes and Redis is written
   - `/api/filter-strategy/optimize` serves frozen picks (zero computation per request)
   - Lightweight 60-second loop monitors game completion
   - On all-final, clear cache and pre-warm tomorrow's pipeline

**Timing Gates (Prevent Mid-Slate Interference):**

- **Manual pipeline endpoints** (`/api/pipeline/fetch`, `/api/pipeline/score`, `/api/pipeline/run`, `/api/pipeline/filter-strategy`) are locked during active slates. If today's slate has unfinished games, these endpoints return HTTP 423 "Locked — pipeline running via T-65 monitor". They only accept calls after all games finish.
- **The `/optimize` endpoint** never calls the pipeline. It serves cache only.
- **Start-up timing guard** (main.py): On app restart during a live slate (after T-65), restore frozen picks from SQLite/Redis instead of purging and regenerating. Prevents mid-game lineup changes due to dyno restarts.

**Why This Architecture Matters:**

1. **Pick Quality**: All candidate fetches, scoring, and optimization happen in one synchronized run with live data. No stale data. No partial updates.
2. **User Predictability**: Picks are generated at T-65 and available immediately after. Users know exactly when to draft.
3. **No Generational Drift**: No risk of serving lineups built from different versions of the schedule (e.g., lineup A built at 6:00 PM from 8 games, lineup B built at 6:15 PM from 7 games after a cancellation).
4. **Testability**: Manual endpoints (`/api/pipeline/*`) exist for post-slate analysis and testing, but are gated and only work after all games finish.

**Mid-slate cold start (app redeploys after the day's first pitch):**

When the app restarts *after* the day's first pitch — e.g. an early afternoon game is already Live when the Railway dyno cycles — the full-slate `first_pitch` timestamp is in the past, so `lock_time = first_pitch − 65 min` is also in the past. The monitor's Phase 2 sleep naturally skips (lock already elapsed) and the pipeline runs **cold, immediately, on the remaining games only**.

Timing logic is unchanged: `_get_first_pitch_utc()` still returns the earliest of ALL games, and `app/main.py`'s startup restore guard still compares `now >= lock_time` against the full-slate first pitch. What changed is that every downstream stage filters to `is_game_remaining(g.game_status)`:

- `fetch_schedule_for_date()` — still ingests ALL games. This is the only stage that sees started games, so `game_status` keeps progressing `Preview → Live → Final` over the day.
- `populate_slate_players()`, `enrich_slate_game_team_stats()`, `enrich_slate_game_series_context()`, `enrich_slate_game_weather()`, `enrich_slate_game_vegas_lines()` — all filter to `is_game_remaining`. The Odds API does not return lines for started games; unfiltered, this was the crash that surfaced as HTTP 503 on the 2026-04-20 mid-slate redeploy.
- `run_score_slate()` and `run_filter_strategy_from_slate()` — skip SlatePlayers whose game has started (belt-and-suspenders for stale DB rows from a prior failed run).
- `run_full_pipeline()` — raises a clean `RuntimeError("Insufficient remaining games (N of M) for <date> — T-65 aborted")` if fewer than `MIN_GAMES_REPRESENTED` (2) games still remain, so the failure is diagnosable rather than surfacing as a cryptic `ValueError` from `_enforce_composition`.

If a Redis-frozen T-65 payload from earlier in the day exists, the standard `restore_and_refreeze` path in `app/main.py` brings those picks back unchanged. The cold pipeline run only fires when no frozen payload can be restored.

One source of truth: `STARTED_GAME_STATUSES = frozenset({"Live", "Final"})` and `is_game_remaining(game_status)` live in `app/core/constants.py`.

**Key Functions:**

- `app.services.slate_monitor.targeted_slate_monitor()` — Main T-65 event loop
- `app.services.slate_monitor._get_first_pitch_utc()` — Parse game times, compute lock time
- `app.services.slate_monitor._sleep_until()` — Chunked async sleep for responsive cancellation
- `app.services.lineup_cache.freeze()` — Freeze picks after T-65 run
- `app.core.utils.is_pipeline_callable_now()` — Gate manual pipeline endpoints
- `app.core.constants.is_game_remaining()` — Shared filter for started-game exclusion

## Data Files (`/data/`)

Current coverage (as of 2026-04-21): **28 slates, 2026-03-25 → 2026-04-21**. All four files stay in lockstep — every date present in one is present in all four.

**Two roles — do not confuse them:**
- **Outcome labels** (`historical_players.csv`, `historical_winning_drafts.csv`) — retrospective results. What players scored, who won, what lineups paid off. Never used as live pipeline inputs.
- **Calibration ground truth** (`hv_player_game_stats.csv`, `historical_slate_results.json`) — what the conditions and player performances actually were. Used to validate that the live scoring signals correlate with real outcomes.

| File | Role | Current size | Contents |
|---|---|---|---|
| `historical_players.csv` | Outcome labels | 1013 rows / 28 dates | Master player ledger: real_score, card_boost, drafts, leaderboard flags (HV/MP/3X). **Null `real_score` / `total_value` = DNP/scratch.** Avg ~36 rows/date (range 22–56). |
| `historical_winning_drafts.csv` | Outcome labels | 985 rows / 28 dates | Top-ranked lineups per date (5 rows per lineup). 3–12 ranks captured per date; target is 20. |
| `historical_slate_results.json` | Calibration ground truth | 28 entries | Per-date game context: game results and scores. Game objects can be extended with env condition fields (Vegas lines, ERA/K9/WHIP/hand, team OPS/K%, bullpen ERA, series context, weather) when needed for manual calibration analysis. Cross-reference with historical_players.csv by (date, team). |
| `hv_player_game_stats.csv` | Calibration ground truth | 446 rows / 28 dates | Actual box scores for every Highest-Value player appearance. Batting (ab, r, h, hr, rbi, bb, so) and pitching (ip, er, k_pitching, decision) coexist — blanks = not applicable. |

## Env Scoring Calibration

The env scoring thresholds in `app/core/constants.py` (BATTER_ENV_VEGAS_FLOOR, ERA floors/ceilings, etc.) are set by reasoning, not automation.

**ABSOLUTE RULE: No automated calibration or training scripts.** Do not create scripts that read historical outcome data (real_score, HV flags, drafts, total_value) and produce threshold recommendations, condition matrices, or any analysis intended to inform scoring parameters. This is a one-way door to data leakage. Any such script that is accidentally run could start a feedback loop where post-game outcomes corrupt pre-game scoring logic.

**How calibration is done:** Manually, by Claude reading `historical_players.csv` and `historical_slate_results.json` directly and reasoning about whether conditions correlate with outcomes. The question is: *when the live pipeline would have rated a condition favorably, did players in those conditions actually score well?* The answer is evaluated by inspection, not by script. If a threshold is clearly miscalibrated (e.g., O/U > 9.5 is favored but RS distribution shows no difference vs mid-range), edit `app/core/constants.py` directly.

**The only permitted script in `/scripts/` that touches `/data/`:** `backfill_slate_results_and_hv_stats.py` — fetches from the MLB Stats API to fill missing box scores and game results. It reads and writes `/data/` files only; it does not touch the live pipeline or DB, and it does not use outcome data as inputs to any scoring logic.

**CI gate:** `scripts/audit_live_isolation.py` — static grep-based scan of `app/` for banned outcome fields (`real_score`, `total_value`, `is_highest_value`, `is_most_popular`, `is_most_drafted_3x`) in runtime code paths. Run before every deploy.

## Ingesting New Slate Data

New slates are ingested **manually by appending rows** to the four files above — there is no automated collector. After a slate completes, capture the platform's leaderboards and append to each file. The canonical column-by-column reference is reproduced below. Keep all four files in lockstep — a date missing from any one of them will break cross-validation.

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

The CSV/JSON files are the source of truth; the SQLite DB is rebuilt from them via `app/seed.py`. `run_seed()` is **idempotency-guarded** — it only seeds if the `weight_history` table is empty (guard at `app/seed.py:263`). To pick up freshly appended rows:

```bash
rm db/ben_oracle.db              # or DROP TABLE in Postgres
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

---

## Improved Ingest Process (V9.1)

This section documents best practices for accurate, repeatable historical data ingestion based on 2026-04-17 slate analysis.

### 1. Player Deduplication Algorithm

**Goal:** Consolidate players appearing in multiple leaderboards into single rows with combined flags.

**Process:**
1. Extract all players from three leaderboards: Most Popular (MP), Highest Value (HV), Most Drafted 3x (3X)
2. Build a dict keyed by `(player_name_normalized, team_abbreviation)`
3. For each unique player, merge all occurrences:
   - Use the best-quality data source (HV > MP > 3X for real_score, card_boost)
   - Take the maximum `drafts` count from any source
   - Set flags: `is_highest_value=1` if in HV list, `is_most_popular=1` if in MP, `is_most_drafted_3x=1` if in 3X
4. Output one row per unique (name, team) pair with combined flags

**Python example:**
```python
players = {}  # (name, team) -> {rs, boost, drafts, is_hv, is_mp, is_3x}
for source, source_list in [("HV", hv_players), ("MP", mp_players), ("3X", _3x_players)]:
    for p in source_list:
        key = (p["name"], p["team"])
        if key not in players:
            players[key] = {**p, "is_hv": 0, "is_mp": 0, "is_3x": 0}
        else:
            # Merge: keep best RS/boost, max drafts
            if source == "HV":
                players[key]["is_hv"] = 1
            elif source == "MP":
                players[key]["is_mp"] = 1
            elif source == "3X":
                players[key]["is_3x"] = 1
            players[key]["drafts"] = max(players[key]["drafts"], p["drafts"])
```

**Verification:**
- Each player appears exactly once per date
- Flag sum per player ∈ [1, 3] (at least one source, up to three)
- No duplicate (date, name, team) rows after consolidation

### 2. Position Inference Guide

**Pitcher identification:**
- Known pitchers (RS ≥ 5.0, or in pitcher-specific lists like "Glasnow", "Suarez", "Soriano"): position = "P"
- All others: position = "OF" (generic outfielder for unknown position batters)
- Manual override: If player identity is ambiguous, check MLB roster or platform UI before assigning position

**Why generic outfielder?**
- Real Sports leaderboards do not always specify detailed positions (SS, 3B, C, etc.)
- Using "OF" avoids false precision; position doesn't affect historical scoring
- When building lineups post-hoc, detailed positions can be backfilled from MLB API if needed

### 3. Slot Assignment Algorithm (Winning Drafts)

**Goal:** Reconstruct the five-slot lineup structure from a list of 5 players (order may be ambiguous).

**Recommended approach (V8.0-aligned):**
1. Identify the pitcher (highest RS, or known pitcher names)
2. Assign pitcher to slot 1 (multiplier 2.0)
3. Sort remaining 4 batters by RS descending
4. Assign batters to slots 2–5 with multipliers [1.8, 1.6, 1.4, 1.2]

**Why this approach?**
- Aligns with V8.0 pitcher-anchor architecture (pitcher in slot 1, always 2.0x)
- Uses RS to break ties, which correlates with draft performance
- Ensures structural consistency across all historical lineups

**Python example:**
```python
pitcher = max(players, key=lambda p: p["rs"])  # Highest RS is likely pitcher
batters = [p for p in players if p != pitcher]
batters.sort(key=lambda p: p["rs"], reverse=True)

slot_mults = [2.0, 1.8, 1.6, 1.4, 1.2]
output_rows = []
output_rows.append((pitcher["name"], pitcher["team"], "P", 1, pitcher["rs"], pitcher["boost"], slot_mults[0]))
for slot, batter in enumerate(batters, start=2):
    output_rows.append((batter["name"], batter["team"], "OF", slot, batter["rs"], batter["boost"], slot_mults[slot - 1]))
```

### 4. Game Result Inference (HV Game Stats)

**Goal:** For each HV player, match their team to the day's games and populate `game_result`.

**Process:**
1. For each HV player, identify their team (e.g., "LAD")
2. Scan the games list for team in either home or away column
3. Build game_result string: `"{home_abbr} {home_score} {away_abbr} {away_score}"`
4. If no game found for that team, log a warning (that team may not have played)

**Format examples:**
- LAD player in LAD 7, COL 1 (home) → game_result = "LAD 7 COL 1"
- SF player in WSH 5, SF 10 (away) → game_result = "WSH 5 SF 10"

**Limitation:** Images do not provide per-player box-score stats (AB, R, H, HR, RBI, IP, ER, K). These columns remain null. Future ingests should use `backfill_slate_results_and_hv_stats.py` to populate from MLB API post-game.

### 5. Capture Checklist (Pre-Ingest QA)

Before appending rows to CSV files, verify:

- [ ] **Player names:** All normalized (accent removal: Á→A, é→e, ó→o)
- [ ] **Teams:** All 3-letter MLB abbreviations (BAL, BOS, LAD, NYY, etc.)
- [ ] **Real scores:** Extracted accurately (negative values allowed for poor performance)
- [ ] **Card boosts:** Numeric values or blank (null → no boost; "—" → 0.0; "2.3x" → 2.3)
- [ ] **Draft counts:** Correctly converted ("1.5k" → 1500, not 150 or 15000)
- [ ] **Formulas:** `total_value = real_score × (2 + card_boost)` verified for each row
- [ ] **Leaderboard lineups:** Exactly 5 players per lineup, pitcher in slot 1 (or documented alternative)
- [ ] **Game results:** 14+ games, all teams valid, scores non-negative
- [ ] **Flags:** Assigned correctly after deduplication (one row per player, multiple flags possible)
- [ ] **Duplicates:** No (date, player_name, team) appears twice in historical_players.csv

### 6. Team Abbreviation Reference

Standard MLB 3-letter codes (by division):

| AL East | AL Central | AL West | NL East | NL Central | NL West |
|---------|-----------|---------|---------|-----------|---------|
| BAL | CWS | HOU | ATL | CHC | ARI |
| BOS | CLE | LAA | MIA | CIN | COL |
| NYY | DET | OAK | NYM | MIL | LAD |
| TB | KC | SEA | PHI | PIT | SD |
| TOR | MIN | TEX | WSH | STL | SF |

**Non-standard names to avoid:** "Oh God" (should be MIL or another team). If uncertain, ask rather than guess.

### 7. Data Quality Gate (Pre-Reseed)

Create and run a validation script before `rm db/ben_oracle.db && python -m app.seed`:

```bash
python scripts/validate_ingest.py --date 2026-04-17
```

This script checks:
- All three CSV files have the new date in lockstep (count match)
- No duplicate (date, player_name, team) in historical_players.csv
- All `total_value` formulas are correct (RS × (2 + boost))
- All `slot_mult` values are in {2.0, 1.8, 1.6, 1.4, 1.2}
- Pitcher count in historical_winning_drafts.csv = 1 per lineup (4 per date)
- Flag counts are consistent (HV + MP + 3X ≤ total unique players)
- real_score in reasonable range (typically -5 to +10, warn on outliers)

**Exit codes:**
- 0: All checks passed, safe to reseed
- 1: Warnings (e.g., unusual values) — review before proceeding
- 2: Errors (e.g., duplicates, formula failures) — fix and rerun

### 8. Estimated Row Counts Per Slate

Use these estimates to flag incomplete ingests:

| File | Minimum | Target | Maximum |
|------|---------|--------|---------|
| historical_players.csv | 20 unique | 30–40 | 60+ |
| historical_winning_drafts.csv | 20 rows (4 lineups) | 50–100 rows (5–20 lineups) | 500+ |
| hv_player_game_stats.csv | 10 rows | 20–30 | 50+ |

If captured lineups < 4 or unique players < 20, flag the ingest as incomplete and document what was missing.

### 9. Full Data Flow Example (2026-04-17)

**Input:** 7 images with leaderboard, HV, MP, 3X, and games data.

**Step 1 — Extract & deduplicate:**
- Extract 4 leaderboard lineups (oshavis, cheezit_man, texastechf4n, adivv) = ~20 player occurrences
- Extract 14 HV players, 14 MP players, 5 3X players
- Merge by (name, team): Tyler Glasnow (LAD) in leaderboard + MP = single row with is_mp=1
- Result: 36–37 unique players

**Step 2 — Build historical_players.csv rows:**
```
2026-04-17,Tyler Glasnow,LAD,P,6.4,0.0,2100,12.8,0,1,0
2026-04-17,Ranger Suarez,PHI,P,7.7,0.4,54,18.48,1,0,0
2026-04-17,Max Muncy,LAD,OF,5.6,3.0,1,28.0,1,0,0
```

**Step 3 — Build historical_winning_drafts.csv rows:**
- Rank 1 (oshavis): T. Glasnow (P) → Slot 1; remaining 4 by RS descending → Slots 2–5
- Rank 2–4: repeat structure
- Result: 4 × 5 = 20 rows

**Step 4 — Build hv_player_game_stats.csv rows:**
- For each of 14 HV players, match team to game
- Populate game_result; leave batting/pitching columns null (no image data)
- Result: 13 rows (1 player's team not in games list)

**Step 5 — Validate & reseed:**
```bash
python scripts/validate_ingest.py --date 2026-04-17
rm db/ben_oracle.db
python -m app.seed
```

### 10. Future Enhancements (Post-V9.1)

**Candidates for automation (do NOT implement in this task):**

1. **CSV Builder Tool** (`scripts/build_ingest_csvs.py`):
   - Accepts JSON input (leaderboards, HV, MP, 3X, games)
   - Auto-deduplicates, assigns slots, populates game_results
   - Outputs three CSVs with validation

2. **Position Lookup Service:**
   - Query MLB API or cached roster for exact position (SS, 3B, C, etc.)
   - Fallback to generic "OF" if not found

3. **Box-Score Backfill:**
   - Post-ingest, run `backfill_slate_results_and_hv_stats.py`
   - Populates AB, R, H, HR, RBI, IP, ER, K from MLB Stats API

4. **Validation Script** (`scripts/validate_ingest.py`):
   - Implement full data quality gate (see section 7 above)
   - Exit with clear error messages on failure

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

`matchup_quality` (V10.6) is a four-sub-signal blend: opp ERA (35%), opp WHIP (20%), batter-vs-handedness OPS split (30%), and a K-vulnerability cross-penalty (15%). The K-vuln signal multiplies a normalised batter K% (`so/pa`, floor 18% / ceiling 30%) by a normalised opp K/9 (floor 7.5 / ceiling 11.0); only the (high × high) corner fires the full penalty. This is the trait-layer answer to "0-for-4 with 3K" risk: a contact-oriented hitter is fine vs an elite K-arm because their bat-to-ball floor protects them, and a high-K hitter is fine vs a contact pitcher because the pitcher won't generate the whiffs to bury him — only the cross is dangerous. Anti-aligned with env Group A6 (opp K/9 alone): env scores the OPPORTUNITY for runs, trait scores the FLOOR for an individual batter. When sub-signals are absent (rookie batter no PA, missing K/9), the blend re-normalises across the available terms.

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

## Graduated Scaling Helpers (`app/core/utils.py`)

The env-score functions use shared helpers instead of duplicated inline patterns (defined at lines 117–143 of `app/core/utils.py`; imported by `filter_strategy.py`):

- `graduated_scale(value, floor, ceiling)` → 0.0–1.0 (works for ascending and descending ranges)
- `graduated_scale_moneyline(moneyline, ml_floor, ml_ceiling)` → 0.0–1.0 (negative-number-aware)

## Popularity (V11.0: REMOVED)

V11.0 removed the popularity scraping module entirely.  The optimizer is
**popularity-agnostic**: don't favor popular players, don't fade unpopular
ones either.  Predict high-value performers from env + trait alone.  See
the V11.0 changelog entry below for the full rationale and the list of
deleted modules.

## Optimizer (`app/services/filter_strategy.py`)

**Strategy Version: V11.0 "Popularity Removed — Single Lineup, Pure Env+Trait"** — The optimizer is built exclusively from information available before any draft begins. Card boosts and platform draft counts are **not optimizer inputs and do not exist on `FilteredCandidate`**. **Popularity (FADE/TARGET/NEUTRAL) and the Moonshot lineup are gone.** The optimizer ranks the candidate pool purely on `env_factor × volatility_amplifier × trait_factor × stack_bonus × dnp_adj`, then assembles a single lineup (1 SP + 4 batters, OR 0 SP + 5 batters chosen by slot-weighted total EV). The dual-lineup architecture, FADE gate, sharp_score scraping, and explosive_bonus all retired. Pitchers cap at env_factor 1.20 (vs batter 1.30). **Stacking is still capped at 2 batters per team AND 2 per game, and only fires on overwhelmingly clear game scripts.**

### V11.0 Popularity Removed + Single Lineup + No /api/draft/evaluate (April 30)

V11.0 is a structural cleanup:

1. **Popularity scraping removed.** `app/services/popularity.py` (Google Trends + ESPN/MLB RSS + Reddit/FanGraphs sharp scrape), `app/routers/popularity.py`, `app/schemas/popularity.py`, the `PopularityClass` enum, the FADE-batter exclusion gate (`_exclude_fade_players`), the FADE-pitcher soft penalty (`PITCHER_FADE_PENALTY`), and the `popularity` / `sharp_score` fields on `FilteredCandidate` are all deleted. The optimizer no longer asks "what does the crowd think" anywhere — it predicts high-value performers from env + trait alone. The crowd-avoidance signals were noisy web-scrape proxies that distorted ranking toward contrarian picks even when env + trait disagreed.

2. **Moonshot retired.** The dual-lineup architecture existed solely to differentiate via popularity (Starting 5 = chalk-tolerant, Moonshot = anti-crowd via sharp + explosive bonuses). With popularity gone, there is no principled second axis — Moonshot would be either identical to Starting 5 or differentiated by an arbitrary axis. `run_dual_filter_strategy`, `_compute_moonshot_filter_ev`, `DualFilterOptimizedResult`, `MOONSHOT_SHARP_BONUS_MAX`, `MOONSHOT_EXPLOSIVE_BONUS_MAX` deleted. Response schema collapsed: `FilterOptimizeResponse.lineup` (single) replaces `starting_5` + `moonshot`.

3. **/api/draft/evaluate + draft_optimizer.py deleted.** The endpoint accepted `card_boost` from the user as a request field and fed it through `compute_expected_value()` → `compute_total_value()` → slot-value math. card_boost is **never knowable pre-draft** (revealed during the draft), so the endpoint was structurally leaking an outcome signal into the rank. Deleted outright: `app/routers/draft.py`, `app/schemas/draft.py`, `app/services/draft_optimizer.py`, `tests/test_draft_optimizer.py`.

4. **Audit script extended.** `scripts/audit_live_isolation.py` now also flags `card_boost` and `drafts` reads in the runtime tree (with router display-map lines explicitly exempt). Plus `popularity` / `sharp_score` / `PopularityClass` to catch any reintroduction.

V11.0 explicitly **does not** change: env scoring (Vegas O/U, opp ERA / WHIP / K/9, bullpen, park, weather, platoon, batting order, ML, series, L10), trait scoring (Statcast kinematics, xStats, framing, opp rest days), the V10.x stacking gates and per-team / per-game caps, the EV-driven 1P+4B vs 0P+5B chooser, V10.8 catcher framing / xStats / opp-rest-days additions, the no-fallbacks rule, or the no-historical-outcome rule. Pure surgical removal of the popularity surface area + the card_boost leak path.

### V10.8 Sustainable Signal Expansion (April 29)

V10.8 is a research-driven add of four pre-game signals, prioritised after a literature scan of DFS best practices.  Decisions were guided by 2026 MLB rule changes (the ABS Challenge System affects framing/umpire signal magnitudes) and by published predictive-power studies.  All additions follow the no-historical-bleed rule — every new field is a factual season aggregate or a derived schedule fact, never an outcome label.

**Sources reviewed:**
- [MLB Glossary on xwOBA](https://www.mlb.com/glossary/statcast/expected-woba)
- [Baseball Savant Expected Statistics Leaderboard](https://baseballsavant.mlb.com/leaderboard/expected_statistics)
- [pybaseball GitHub](https://github.com/jldbc/pybaseball)
- [MLB.com 2026 ABS Challenge System announcement](https://www.mlb.com/news/abs-challenge-system-mlb-2026)
- [TruMedia Catcher Framing model — 3.9% K-rate per framing run](https://baseball.help.trumedianetworks.com/baseball/catcher-framing-model)
- [FanGraphs — The Effect of Rest Days on Starting Pitcher Performance](https://community.fangraphs.com/the-effect-of-rest-days-on-starting-pitcher-performance/)
- [FantasyLabs — Opponent Rest Days impact on DFS](https://www.fantasylabs.com/articles/how-does-a-well-rested-opponent-affect-hitters-and-pitchers/)

**Five changes (one re-weight + four new signals):**

1. **Vegas O/U weight 1.0 → 0.5 (`BATTER_ENV_VEGAS_WEIGHT`).** The V10.7 fresh-eyes audit showed Vegas O/U is essentially flat at the player level (Q1 mean_rs 2.35 vs Q4 2.62 — a 1.04× swing).  Pre-V10.8 it was a weight-1.0 PRIMARY signal in batter env Group A alongside ERA / ML / bullpen.  Down-weighting to 0.5 (matching WHIP's contribution) removes the over-weighting without losing the small signal entirely — O/U bakes in BOTH teams' offenses, so an individual batter's RS upside is only weakly correlated with the total.  Direct opp-pitcher signals (ERA, WHIP, K/9) carry the actual matchup-specific information.

2. **Statcast xStats — xwOBA / xBA / xSLG (batters), xERA / xwOBA-against (pitchers).** Industry-standard predictive metrics derived from Statcast batted-ball data (exit velocity + launch angle + sprint speed for some plays).  xwOBA is more predictive than realised wOBA on small samples; xERA flags regression candidates when a pitcher's live ERA disagrees with his expected ERA (FantasyLabs / PitcherList: "1.00+ run gap is a screaming regression signal").  New PlayerStats columns: `x_woba`, `x_ba`, `x_slg`, `x_era`, `x_woba_against`.  Refresh path: `scripts/refresh_statcast.py::refresh_batter_expected_stats` and `refresh_pitcher_expected_stats` pull from pybaseball's wraps of Savant.  Wired into `score_power_profile` (xwOBA gets 4 of the 25 points, replacing HR/PA which the MLB API never reliably populated) and `score_pitcher_era_whip` (xERA blended at 25% with live ERA + WHIP).

3. **Pitch-arsenal mismatch via opp xwOBA-against (simplified V10.8 take).** Full per-pitch-type batter-vs-pitcher arsenal modelling (`statcast_pitcher_pitch_arsenal` × `statcast_batter_pitch_arsenal`) is a deep wiring task with high implementation cost.  V10.8 ships the simplified version: the opposing pitcher's overall **xwOBA-against** captures arsenal effectiveness in one number — "is this pitcher's arsenal suppressing contact quality?".  Wired into `score_batter_matchup` as a 5th sub-signal at 10% weight (era 30% + whip 18% + hand-split 27% + K-vuln 15% + arsenal 10%).  When the opposing pitcher has no Savant row (rookie pre-50 PA), the trait falls through to the V10.6 four-sub-signal blend.  Future enhancement: full per-pitch-type modelling (deferred — see "What's deferred" below).

4. **Catcher framing — small ±5% adjustment to pitcher k_rate.** Research model: each framing run/game ≈ 3.9% K-rate impact (TruMedia).  Top team framers run ~10-15 runs/season; we cap at ±5% to be conservative.  **2026 ABS Challenge System reduces the magnitude** because challenged calls auto-correct, but only ~2% of pitches get challenged per game (each team has 2 challenges) — so the ~98% unchallenged pitches still carry a real framing effect.  Implementation: new `TeamSeasonStats` table (per-team season aggregates), populated by `scripts/refresh_statcast.py::refresh_team_catcher_framing` from the embedded JSON of Savant's team framing leaderboard.  `candidate_resolver` and `pipeline.run_score_slate` build a `team_framing_lookup` dict per slate and pass it to `score_pitcher_k_rate`, which scales the trait via `_apply_framing_adjustment`.  Best-effort: framing scrape failure logs and falls through to neutral; the rest of the pipeline runs.

5. **Opponent rest days — back-to-back bonus to batter env.** Research: the BATTER's edge comes from the OPPOSING team's rest, not their own.  FantasyLabs DFS data shows hitters facing teams with 0 rest days (back-to-back game) do measurably better — depleted opposing bullpen + tighter starter pitch leash.  New SlateGame columns: `home_team_rest_days`, `away_team_rest_days`.  Derived from the existing schedule lookback in `enrich_slate_game_series_context` (zero new MLB API calls — same data we already pull for L10 / series wins).  Wired into batter env Group A as a +0.2 bonus (binary, no graduated scale) when `opp_team_rest_days <= 0`.

**What's deferred / skipped (and why):**

- **Umpire K/BB tendencies — SKIPPED.**  Under the 2026 ABS Challenge System, the strike zone is still ~98% human-called but worst calls are corrected by challenges.  The umpire-level K/BB delta we'd score against is significantly compressed.  Implementation cost is high (need umpire ID per game from MLB API + a separate umpire stats database); ROI under ABS is low.  Will revisit if Year-1 ABS data shows a meaningful residual umpire effect.
- **Pitcher's own rest days — SKIPPED.**  FanGraphs research: short rest (1-3 days) vs normal rest (4-6) vs extended rest (7+) shows no significant difference in starting pitcher performance.  Cumulative pitch-count workload from prior 5-10 games matters more, but adding that signal is a separate wiring effort.  Skipping the simpler "own rest days" form because the published evidence says it's not predictive.
- **Full per-pitch-type pitch-arsenal mismatch — DEFERRED.**  Savant publishes batter wOBA / K% by pitch type AND pitcher pitch usage by type.  Computing the full mismatch requires storing per-(player, pitch_type) rows on a new table and joining the cross-product at scoring time.  Higher implementation cost than V10.8 budgets.  V10.8 ships the simplified "arsenal effectiveness via xwOBA-against" version; full mismatch is a Phase B enhancement.

**Limitation: empirical validation requires historical backfill.**  The eval harness reads pre-game features from `historical_slate_results.json`, which doesn't have the new V10.8 fields (xStats, framing, opp rest days).  So the 33-slate audit can't currently bucket-rate HV outcomes against these signals — V10.8 is shipping on industry-research validation, not our own empirical check.  Backfill would require: (a) one-time `backfill_slate_results_and_hv_stats.py` extension to enrich historical_slate_results.json with current xStats / framing values (close-enough since these stabilise over the season; not point-in-time exact), (b) re-run of the bucket audit on the new fields to confirm direction, (c) constant tweaks if any signals are inverted.  The user opted to ship V10.8 first and validate retrospectively in a later pass.

**Eval delta (V10.7 → V10.8 baseline, env+pitcher-trait variant on 33 slates):**

| Metric | V10.7 | V10.8 |
|---|---|---|
| HV@20 | 9.15 | 9.18 (≈ flat — new signals don't fire in eval w/o backfill) |
| TV@20 | 11.45 | 11.58 |

V10.8 is essentially neutral on the eval because the new fields aren't in the historical data — the addition's correctness is verified by tests, code review, and adherence to research best practices.  Live impact will be visible once `refresh_statcast.py` populates the new tables on the next slate cycle.

V10.8 explicitly **does not** change: pitcher env ceiling 1.20 (V10.6), V10.7-neutralised L10 / series momentum / heavy-fav pitcher ML curves, FADE bifurcation (V10.5), EV-driven 0P+5B chooser (V10.5), composition or stacking rules.

### V10.7 Fresh-Eyes Feature Audit (April 29)

V10.7 ships three calibration changes from a fresh-eyes feature audit on the same 33-slate corpus.  The audit ignored model assumptions and asked the data directly: which pre-game features actually correlate with HV outcomes and mean real_score?  The harness lives at `/tmp/baseball_eval/feature_audit.py` (manual analysis, never committed — per the no-historical-outcome-script rule).  Results bucketed each feature into quartiles and reported HV-rate + mean RS per bucket.  Three signals turned out to be **inverted** vs the live model's assumed direction:

1. **Pitcher's team moneyline — peak at mild favorite, not heavy favorite (`PITCHER_ENV_ML_CEILING` -220 → -150).**  The bucket analysis decisively flipped the ML curve.  Q1 (heavy fav, ML -310 to -168): mean_rs **3.12** / HV-rate **12.7%**.  Q2 (mild fav, ML -164 to -120): mean_rs **4.20** / HV-rate **38.2%**.  Heavy-favorite pitchers underperform because their teams generate blowouts → starter pulled in the 5th-6th inning before K total / win-bonus stack up.  Mild favorites stay in tight games and pitch deeper.  The pre-V10.7 curve gave full ML credit at -220, increasing monotonically through the heavy-fav tail; the new curve saturates at -150 so additional ML weight stops being added once we're past the mild-favorite peak.

2. **Team L10 wins — INVERTED at the player level (`TEAM_HOT_L10_BONUS` 0.4 → 0.0, `TEAM_COLD_L10_PENALTY` 0.4 → 0.0).**  Q1 (cold, 0-4 wins): mean_rs **2.86**.  Q4 (hot, 7-10 wins): mean_rs **2.40**.  Cold teams produce MORE individual RS upside, not less — likely because hot teams have multiple contributors so HV is spread thin, while cold teams have one star carrying the offense (regression candidate).  The V10.2 doubling of L10 bonuses (0.2 → 0.4) actively moved env-scoring in the wrong direction.  Neutralised to 0.0 rather than reversed because the inversion is real on 33 slates but the magnitude could regress with more data; flipping a sign on small-sample evidence is risky, removing the signal entirely is conservative.

3. **Series lead — INVERTED at the player level (`SERIES_LEADING_BONUS` 0.6 → 0.0, `SERIES_TRAILING_PENALTY` 0.6 → 0.0).**  Series-trailing batters HV-rate 55.1% vs leading 44.8% — same inversion mechanism as L10.  Trailing teams are still taking PAs in must-score-now situations while leading teams ride bench bats.  Neutralised, not reversed, for the same risk-management reason.

`BATTER_ENV_MAX_SCORE` dropped from 6.0 → 5.0 to match the new max (Group D is now structurally 0.0 — no momentum bonus or penalty).  Without this rebase every batter's env_score would silently shrink ~17% as a normalisation artefact, dragging the whole batter pool down vs pitchers and undoing V10.6's parity work.  Group A can still saturate (~2.625 with WHIP+K9 contributions through the soft-cap slope) so perfect-storm batter games hit env_score=1.0; the `min(1.0, total/5.0)` clamp preserves the upper bound.

Eval impact (env+pitcher-trait variant, 33 slates):

| Metric | V10.5 baseline | V10.6 | **V10.7** | User target |
|---|---|---|---|---|
| HV@20 | 8.24 / 20 | 8.73 / 20 | **9.15 / 20** | 8–9 ✓ above |
| HV@10 | 3.85 | 4.09 | **4.15** | — |
| TV@20 | 10.82 | 11.15 | **11.45** | — |

V10.7 explicitly **does not** change: pitcher env ceiling 1.20 (V10.6), opp-K/9 batter env Group A6 (V10.6), env-conditional volatility amplifier (V10.6), batter K-vulnerability trait sub-signal (V10.6 follow-up), DNP_UNKNOWN_PENALTY 0.93 (V10.6), the FADE bifurcation (V10.5), the EV-driven 0P+5B chooser (V10.5), or any structural rule (composition, stacking, anchor logic).

### V10.6 Pitcher-Batter Parity (April 28-29 — env eval cycle)

V10.6 ships four targeted refinements from an offline evaluation of every V10.5 component on the 33-slate enriched corpus. The harness compared the live env-scoring engine's pre-game ranking against actual top-20-by-total-value and `is_highest_value` outcomes. Baseline metrics: TV@20 = 10.82/20 (54.1%), HV@20 = 8.24/20 (41.2%), with pitchers occupying 54% of the model's top-10 (target ~40%) and batter same-team ties dominating the rest of the pool. Post-V10.6 metrics: TV@20 = 11.09/20 (+1.4 pp), HV@20 = 8.52/20 (+1.4 pp), HV@5 = 1.91/5 (+21% relative), pitcher share dropped to 39.7%. Diagnostic CSVs and the harness itself live in `/tmp/baseball_eval/` (manual analysis tool, not committed to the repo per the no-historical-outcome-script rule).

1. **Asymmetric env ceiling — pitchers cap at 1.20, batters at 1.30 (`PITCHER_ENV_MODIFIER_CEILING = 1.20`).** Pre-V10.6 both used `ENV_MODIFIER_CEILING = 1.30`. The harness showed any confirmed favored-team starter trivially saturated all 5 pitcher env signals (weak opp OPS + high opp K% + own K/9 + pitcher-friendly park + heavy ML), giving env_factor 1.30 → EV 130 — beating every batter in the pool because batter Group A is soft-capped before reaching saturation. Tightening pitcher EV to 1.20 means batters in genuinely strong run environments (Coors shootouts, weak-bullpen games, hot-streak teams) compete on EV with the dominant favorite-team SP. Pitcher floor is unchanged at 0.70 — bad-env pitchers still get priced out symmetrically. Implemented in `_compute_base_ev` via `env_ceiling = PITCHER_ENV_MODIFIER_CEILING if candidate.is_pitcher else ENV_MODIFIER_CEILING`.

2. **Opposing-starter K/9 added as Group A A6 signal in batter env (NEW SIGNAL — `BATTER_ENV_OPP_K9_*`).** Previously absent — a glaring gap surfaced by the harness. K/9 is a strikeout-rate signal: a high-K starter (≥10 K/9) suppresses contact regardless of his ERA/WHIP, so even mid-tier batters in run-friendly games (high O/U, weak bullpen) underperform when the starter is mowing them down for 6 innings. Conversely a low-K starter (≤6 K/9) means more balls in play = more BABIP variance + more counting-stat upside. Anti-aligned vs the pitcher's own scoring path: the same K/9 number that earns the pitcher full env credit zeros out the opposing batters' contact upside. Floor=10.5 / ceiling=6.5 (descending: lower K/9 = better for batter), weight=0.4 (slightly less than WHIP's 0.5 because K/9 correlates with ERA more than WHIP does — soft cap absorbs the redundancy when ERA and K/9 agree). Wired through `compute_batter_env_score`, `candidate_resolver.py`, and `pipeline.py::run_filter_strategy_from_slate`.

3. **Volatility amplifier env-CONDITIONAL** (`_compute_base_ev`). Pre-V10.6 the formula was `1.0 + cv × BATTER_FORM_VOLATILITY_MAX` — always boosted volatile boom-bust hitters regardless of context. The harness diagnostic surfaced this as the "model loves Max Muncy" pattern: a high-CV batter on a heavy-favorite team in any decent matchup got the full +20% amplification, but the same batter in a tough K-pitcher matchup got the same +20% even though boom-bust profiles are exactly the wrong fit when the env is poor (they bust, hard). New formula scales the amplifier by env deviation from neutral: `amp = 1 + cv × MAX × (env_score − 0.5) × 2`. Volatile batter in great env (env=1.0) → +20%; in neutral env (env=0.5) → 1.0×; in bad env (env=0.0) → −20% penalty. Steady batters (cv≈0) are unaffected. Pitchers never carry `recent_form_cv`, so they default to 1.0 (no change).

4. **`DNP_UNKNOWN_PENALTY` 0.85 → 0.93.** When the constant was set, batting orders were rare at T-65 and 15% reflected genuine uncertainty. V10.4 wired RotoWire expected-lineup scraping which now covers ~90% of teams at T-65, so `batting_order=None` correlates much more with "RotoWire missed this team" than "this batter isn't starting." The harness showed batters were systematically out-ranked by the dominant pitcher pool — every batter paid 0.85 even when env conditions were strong. Reducing to 7% lets confirmed-team batters in good env situations compete on EV with the favorite-team SP. CONFIRMED_BAD (lineup published, player absent) remains at 0.70 — that's still a real signal.

V10.6 explicitly **does not** change: lineup composition (still EV-chooser between 1P+4B and 0P+5B), per-team or per-game caps, the FADE bifurcation (FADE batters out, FADE pitchers soft-penalty), the stack-eligibility two-path rule, the `STACK_BONUS` 1.20×, or any V10.5 structural rule. Only the pitcher env ceiling, the new K/9 signal, the volatility amplifier formula, and the DNP-unknown haircut change.

### V10.5 EV-Driven Composition + Bifurcated FADE + Bug Fixes (April 28 evening)

V10.5 ships three behavior changes and two bug fixes from a holistic re-read of yesterday's outcomes (4/27) and tonight's pre-lock pipeline output. The motivation: 4 of yesterday's top 5 winning lineups had ZERO pitchers, and the FADE gate kept eliminating Ohtani/Yamamoto/Fried-class confirmed probable starters of heavy ML favorites.

1. **0-pitcher lineups allowed (EV decides per lineup).** Pre-V10.5 the structural rule was `REQUIRED_PITCHERS_IN_LINEUP = 1` — every lineup pinned a pitcher to slot 1. Yesterday's top 5 winners on Real Sports had compositions: 5B / 5B / 1P+4B / 5B / 5B. The 1P+4B path was structurally incapable of producing the dominant shape. V10.5 builds BOTH variants (1P+4B with the highest-EV pitcher, and 0P+5B from the top-5 batters) and returns the higher slot-weighted total EV. Tiebreak goes to the pitcher variant (`anchor_ev >= pure_ev`) — conservative default keeps the V5.0 pitcher-anchor identity unless 5B truly dominates. New helpers in `app/services/filter_strategy.py`: `_build_pure_batter_lineup`, `_lineup_total_ev`, `_build_best_variant`. `_smart_slot_assignment` extended to handle the 0-pitcher case (highest-EV batter takes slot 1). `_validate_lineup_structure` relaxed: pitcher-count invariant is now "at most 1" (was "exactly 1"). `REQUIRED_PITCHERS_IN_LINEUP = 1` is now a max, not a required count. The same EV-driven chooser runs independently for Starting 5 and Moonshot — both can go pure-batter on the same shootout slate.

2. **FADE pitcher soft penalty (vs hard exclusion).** Pre-V10.5 `_exclude_fade_players` removed every FADE candidate before EV. Empirically this kept eliminating confirmed probable starters of heavy ML favorites — Ohtani (LAD ML -290 tonight), Yamamoto, Fried, etc. — because pitcher outcomes are one-player-dependent and the crowd correctly piles onto them. CLAUDE.md V8.0 strategy doc explicitly notes the pitcher TARGET-vs-FADE differential is 1.4× (vs the batter 3.0× swing), so applying the same exclusion gate to pitchers as to batters was over-correction. V10.5: FADE batters are still excluded (`_exclude_fade_players` still drops them — the data shows the crowd is ~3× wrong about batter ownership). FADE pitchers stay in the pool and pay `PITCHER_FADE_PENALTY = 0.85` in `_compute_base_ev` — a 15% haircut that requires a FADE pitcher to clear ~18% more env+trait juice to displace a TARGET/NEUTRAL pitcher. Strong pitchers (good env + good traits) still win; weak FADE pitchers don't. Tests: `test_all_pitchers_fade_kept`, `test_fade_pitcher_pays_ev_penalty`.

3. **L10 abbreviation bug fix.** `enrich_slate_game_series_context` was reading `team.abbreviation` from the MLB `/schedule` API response, but the call only hydrates `linescore` — the team object only contains `id`, `name`, `link`. Every team's L10 wins silently computed to 0, killing the Group D recent-form signal across every batter (every batter showed `"Cold team (L10: 0-10)"` in tonight's pre-V10.5 response). Fix: new `TEAM_ABBR_BY_MLB_ID = {v: k for k, v in TEAM_MLB_IDS.items()}` reverse lookup in `app/core/mlb_api.py`; new `_team_abbr_from_mlb()` helper in `data_collection.py` resolves abbreviation from `team.id` when the API doesn't hydrate the field. Verified live against the API.

4. **`stackable_games` OU gate fix.** `classify_slate()` PATH 1 was checking ML only, not OU — so LAD-style "ML -290 / OU 7.5" games were appended to `slate_classification.stackable_games` (display + STACK_BONUS) even though they failed the proper `is_stack_eligible_game()` gate (which the optimizer's per-team-cap path ran correctly). V10.5 adds the OU check inline so display, STACK_BONUS, and per-team caps all use the same dual-gate definition. Test: `test_blowout_with_low_ou_not_stackable`.

V10.5 explicitly **does not** change: stack-eligibility logic (still PATH 1: ML ≤ -200 AND OU ≥ 9.0; PATH 2: OU ≥ 10.5), per-team or per-game caps (still 2/1/2), the anti-correlation guard (anchor's opposing batters still blocked when there IS an anchor), batter EV formula, or any V10.4 calibration constant. Only the FADE gate, the lineup-shape choice, and the two bugs change.

### V10.4 Pre-Card Lineup Harvesting + Decoupled Batter ML (April 28)

V10.4 ships three changes from a holistic re-read of all 33 slates × 354 games. No structural changes — same V10.1 lineup composition (1P + 4B, mini-stack ceiling 2, per-game cap 2).

1. **RotoWire expected lineups (`app/core/rotowire.py`).** The MLB Stats API only exposes lineup cards 30-60 min before first pitch — typically *after* the T-65 lock. Before V10.4, ~95% of batters at T-65 had `batting_order=None` and got mass-haircut by `DNP_UNKNOWN_PENALTY` (0.85), neutralising the Group B "lineup_position" signal across the entire pool. V10.4 scrapes RotoWire's daily-lineups page (the de-facto source for every open-source MLB DFS optimizer — there is no free first-party API) and pre-fills `SlatePlayer.batting_order` from beat-reporter projections. The official MLB API boxscore (Phase 2 of `populate_slate_players`) overrides RotoWire as ground truth when posted; `batting_order_source` records provenance (`"rotowire_confirmed"` / `"rotowire_expected"` / `"official"`). RotoWire failures are best-effort — they log loudly but don't crash the pipeline (graceful degradation, not a forbidden fallback). See `app/services/data_collection.py::_enrich_batting_order_from_rotowire` for wiring; tests in `tests/test_rotowire.py`. Migration: `b2c3d4e5f6a7_add_batting_order_source.py`.

2. **Batter ML decoupled from pitcher ML.** Pre-V10.4 `BATTER_ENV_ML_FLOOR/CEILING` was aliased to `PITCHER_ENV_ML_*` (-130 → -220), inherited as a V10.2 convenience. The 33-slate × 354-game outcome data shows the curve was inverted for batters:

   | Favorite ML bucket | Games | HV per game | vs baseline |
   |---|---|---|---|
   | pickem (no fav) | 23 | 0.96 | -21% |
   | **-110 to -139 (mild)** | 180 | **1.32** | **+8%** ← peak HV |
   | -140 to -169 | 113 | 1.27 | +4% |
   | -170 to -199 | 72 | 1.17 | -4% |
   | **-200 to -250 (strong)** | 22 | **1.14** | **-7%** ← lowest |
   | ≤-250 (extreme) | 11 | 1.55 | +27% |

   The pre-V10.4 curve gave full ML credit at -220 (one of the lowest-HV buckets) and zero credit at -120 (the peak). V10.4 recenters: `BATTER_ENV_ML_FLOOR = -100`, `BATTER_ENV_ML_CEILING = -180`. Mild favorites now get partial-to-full credit; heavy favorites still saturate but aren't disproportionately rewarded. Reasoning: ML is a "team wins" signal — for batters, that's redundant with the opposing-starter ERA signal we score directly via `BATTER_ENV_ERA_*`. ML adds the most marginal signal in the mild-favorite zone where the game stays competitive (more PAs, more late-inning leverage), not in extreme blowouts. `PITCHER_ENV_ML_*` is unchanged — pitchers genuinely benefit from heavier favorites because win-bonus probability scales with ML. PATH 1 stack-eligibility (raw ML ≤ -200) is unchanged. Tests: `test_v10_4_mild_favorite_gets_ml_credit`, `test_v10_4_batter_ml_saturates_at_180`.

3. **Production hardening — silent gather skips converted to raise.** `populate_slate_players` (roster fetch), `enrich_slate_game_team_stats` (batting/pitching), `enrich_slate_game_series_context` (schedule) previously logged + skipped on `asyncio.gather` exceptions, silently dropping a team's data and corrupting downstream env scoring. Per the no-fallbacks rule these now raise `RuntimeError`. Regression tests in `tests/test_smoke.py::TestNoFallbacksOnEnrichment`.

### V10.3 Calibration (April 27 — opp WHIP, wind IN penalty, Statcast IVB)

V10.3 added three signal refinements (no structural changes):

1. **Opposing-starter WHIP added to Group A run-environment.** Cross-tab on the 33-slate history showed WHIP correlates with ERA at r=0.816 but adds modest independent signal in the corners (low-ERA/high-WHIP starters get hit; high-ERA/low-WHIP starters stabilise). Constants: `BATTER_ENV_OPP_WHIP_FLOOR = 1.10`, `BATTER_ENV_OPP_WHIP_CEILING = 1.40`, `BATTER_ENV_OPP_WHIP_WEIGHT = 0.5` (half of ERA's 1.0 saturation contribution — the Group A soft cap absorbs remaining redundancy when ERA and WHIP agree).

2. **Wind direction symmetrised.** Previously only OUT was scored; IN was treated identical to neutral cross-wind. HV-rate analysis: wind OUT 52.9%, neutral 48.0%, wind IN 45.8% — IN suppresses HV by ~2.2 pts (vs OUT's +4.9 pts boost). Constant: `BATTER_ENV_WIND_IN_PENALTY = 0.2` (half the OUT bonus, mirroring the asymmetric magnitude). Floor on `venue` at 0.0 prevents over-penalising in cold/pitcher-park compound cases.

3. **Statcast IVB fix.** Pitcher induced-vertical-break column in the Savant pull was reading the wrong field; corrected so high-IVB ride fastballs are properly weighted in the kinematic k_rate score.

### V10.2 Calibration Changes (April 27)

V10.2 keeps every V10.1 structural rule (1P + 4B, mini-stack ceiling 2, per-game cap 2, no opposing batter in anchor's game). The changes are calibration-only, driven by reading the env-enriched 33-slate history (Mar 25 → Apr 26):

1. **Two-path stack eligibility.** Replaced the single AND-gated rule with an OR of two paths (single source of truth: `is_stack_eligible_game()` in `app/core/constants.py`):
   - **PATH 1 (blowout favorite, favored side only)** — `moneyline ≤ STACK_ELIGIBILITY_MONEYLINE` (−200) AND `O/U ≥ STACK_ELIGIBILITY_VEGAS_TOTAL` (9.0). Earns `STACK_BONUS` (1.20× EV).
   - **PATH 2 (extreme shootout, both sides eligible)** — `O/U ≥ STACK_ELIGIBILITY_SHOOTOUT_TOTAL` (10.5), moneyline-agnostic. Both teams' batters are stack-eligible (2-cap mini-stack) but neither team earns `STACK_BONUS` — they're in a high-run game, not a predictable blowout.
   - Empirical motivation: across 33 slates, ~3-5 PATH 2 games per slate produced 4-7 HV batters each but were missed by V10.1's stricter gate. Apr 23 SD@COL (O/U 12.0, ML −162, 18 actual runs) had 7 HV players spanning both teams; Apr 26 SD@ARI (O/U 15.5, ML −116, 19 actual runs) had 5. Coors-class shootouts are "glaringly obvious" by O/U alone — both lineups feast regardless of which side wins.
   - `StackableGame.is_blowout_favorite` distinguishes the two paths so STACK_BONUS stays gated to PATH 1.

2. **Pitcher moneyline floor/ceiling widened.** `PITCHER_ENV_ML_FLOOR: -110 → -130`, `PITCHER_ENV_ML_CEILING: -250 → -220`. Across the 33-slate window, ~25% of HV pitchers pitched for coin-flip or mild-favorite teams (ML between −150 and +200): Joe Ryan +128, Soriano +160, Gavin Williams +194, Mick Abel +148, Tyler Mahle +152, Lorenzen +184, etc. The old floor zeroed-out moneyline credit for any pitcher whose team wasn't already favored, mathematically discounting K-upside aces in tossup games. The new band gives partial credit at coin flips and full credit at clear favorites (not just blowouts). Aliased to `BATTER_ENV_ML_*`.

3. **L10 momentum bonus/penalty doubled.** `TEAM_HOT_L10_BONUS: 0.2 → 0.4`, `TEAM_COLD_L10_PENALTY: 0.2 → 0.4`. Hot-streak teams consistently produced HV batters (Apr 26 ATL 8-2 in L10 → 6-run home win + 0 ATL HV but multi-game pattern); the old 0.2 contribution was a 3% env swing, below the noise floor. `BATTER_ENV_MAX_SCORE` correspondingly bumped from 5.8 to 6.0 (max momentum is now 1.0 = 0.6 series-leading + 0.4 hot-L10).

V10.2 explicitly **does not** change: lineup composition (still 1P + 4B), per-team cap (still 2 for stack-eligible, 1 otherwise), per-game cap (still 2), opposing-batter prohibition (still on the anchor's game only), or any V10.1 structural rule. The off-limits "4-batter team stack" and "4P + 1B" composition options remain off-limits.

### V10.1 Structural Changes (April 21)

V10.1 kept the correlation edge of stacking but restricted the blast radius. Two gates must BOTH be satisfied for a team to contribute more than one batter:

- Moneyline ≤ `STACK_ELIGIBILITY_MONEYLINE` (−200) — a genuine blowout favorite
- Vegas O/U ≥ `STACK_ELIGIBILITY_VEGAS_TOTAL` (9.0) — a high-run game script

V10.2 retains this rule as PATH 1 of `is_stack_eligible_game()` and adds the shootout PATH 2 (see above).

Even when the gate is cleared, the per-team cap is **2** (`MAX_PLAYERS_PER_TEAM_BATTERS_STACKABLE`) — a mini-stack, never a full 4-man team stack. An independent per-game cap of 2 (`MAX_PLAYERS_PER_GAME_BATTERS`) prevents mixed-side clumps (2 from team A + 2 from team B in the same game). Every other team stays capped at one batter per lineup. `is_stack_eligible_game()` in `app/core/constants.py` is the single source of truth for the gate.

V10.1 preserves all V10.0 structural fixes:

1. **Statcast kinematics wired — with a pre-T-65 in-monitor refresh, NOT a T-65 fetch.** `scripts/refresh_statcast.py` bulk-loads three Baseball Savant leaderboards via `pybaseball` — exit-velo + barrels (batters), percentile-ranks + arsenal-velocity (pitchers) — and upserts the kinematic columns onto PlayerStats. **It runs inside the slate monitor's Phase 2**, fired as a detached `asyncio.create_task` at the start of the T-65 sleep window (`app/services/slate_monitor.py::_refresh_statcast_background`). On a typical day Phase 2 has hours of runway before T-65, so the ~60 s Savant bulk-load finishes long before the lock; if Savant hangs, the refresh keeps running in the background but the T-65 lock fires on time using whatever Statcast data was last persisted. When a row is missing on the leaderboard (new call-up, pre-50 BBE), columns stay NULL and the scoring engine routes through its non-Statcast fallback path. No fixed UTC schedule, no separate Railway cron service — the refresh naturally piggybacks on the slate cycle.
2. **Sacramento (ATH) park factor corrected.** Raised from 0.90 (pre-season guess) to 1.09 (observed 2026 Statcast PF of 1.091 — short RF porch).
3. **Leadoff slot no longer penalised.** `score_lineup_position` gives spots 1-4 equal max points (all top-of-order volume tiers).
4. **card_boost / drafts removed from `FilteredCandidate`.** The optimizer is structurally incapable of consuming them. The router layer joins them from the source `FilterCard` for display only.
5. **`app/services/condition_classifier.py` deleted.** Was unused — only exported entropy/Gini helpers on ownership data.
6. **`MOONSHOT_SAME_TEAM_PENALTY` deleted.** Moonshot naturally diverges from Starting 5 via `sharp_bonus × explosive_bonus`.
7. **`run_filter_strategy_from_slate` call site fixed** (V10.1 patch) — `pipeline.py` no longer passes `card_boost=` to the `FilteredCandidate` constructor; the display join uses a `(name, team) → card_boost` lookup built from `SlatePlayer`.
8. **Rookie Arbitrage baseline** (V10.1 patch) — `score_power_profile` and `score_pitcher_k_rate` now return `UNKNOWN_SCORE_RATIO × max_pts` (neutral baseline) when the player has zero MLB stats AND no Statcast row. Previously both returned 0, which mathematically benched MLB-debut rookies regardless of matchup. Strategy doc §"Rookie Variance Void" — the crowd fades rookies, so we let env/popularity/park decide.

### V10.0 Core Architecture (Popularity Gate + Env/Trait EV + Statcast + Stacking)

**Popularity gate (applied first, before any EV):**
```
candidates = [c for c in candidates if c.popularity != FADE]
```
FADE players never reach EV scoring. TARGET and NEUTRAL players pass the gate and are scored identically — no popularity bonus or penalty in EV.

**The EV formula:**
```
base_ev = env_factor × volatility_amplifier × trait_factor × context × 100
```

| Signal | Source | Range | Role |
|---|---|---|---|
| env_factor (batters) | Pre-game conditions (Vegas O/U, opp ERA, opp WHIP, **opp K/9 V10.6**, bullpen ERA, park, weather, platoon, batting order, moneyline, series context) | 0.70–1.30 | **Primary** — 1.86× swing |
| env_factor (pitchers) | Same env scoring path (own K/9, opp OPS, opp K%, ML, park, home) | 0.70–1.20 | **V10.6 asymmetric ceiling** — pitcher 1-player dependence + Group A non-soft-cap saturation made 1.30 over-reward favorite-team SP |
| volatility_amplifier | Coefficient of variation of recent at-bat production (`recent_form_cv` from scoring engine, batters only — pitchers default 1.0) | 0.8–1.2 (`BATTER_FORM_VOLATILITY_MAX = 0.20`) | **V10.6 env-CONDITIONAL boom-or-bust amplifier** — boost in good env, penalty in bad env |
| trait_factor | Scoring engine (FB velo/IVB/extension/whiff%/chase% for SP; avg EV/hard-hit%/barrel%/max EV for batters; 0-100) | 0.85–1.15 | **Secondary** — 1.35× swing |
| context | stack_bonus × dnp_adj × pitcher_pop_penalty | varies | Situational modifiers (V10.6 dnp_adj: 0.70 / 0.93 / 1.0; V10.5 pitcher_pop_penalty: 0.85 if FADE pitcher, else 1.0) |

The volatility amplifier (V10.6) is `1 + cv × BATTER_FORM_VOLATILITY_MAX × (env_score − 0.5) × 2`. `recent_form_cv` is read from the `recent_form` trait metadata (set in `scoring_engine.py` as `std/mean` of recent per-game production). Pitchers default to 1.0 since CV doesn't apply. Rationale: a hitter with steady singles output has low CV → no amplification; a hitter with multi-HR games sandwiched between 0-fers has high CV → env signals get amplified BOTH WAYS — they're rewarded when env is genuinely strong but penalised when env is weak. Pre-V10.6 the amplifier was unconditional `1 + cv × 0.20`, which over-loved Muncy/Judge/Yordan-class profiles even on slates where their actual env was poor.

**Moonshot differentiation** — same candidate pool as Starting 5, but a different formula:
```
moonshot_ev = base_ev × sharp_bonus × explosive_bonus
```
- `sharp_bonus`: up to +35% from underground analyst buzz (Reddit, FanGraphs, Prospects Live)
- `explosive_bonus`: up to +20% from power_profile (batters) or k_rate (pitchers)
- Player overlap with Starting 5 is allowed; formula divergence naturally reorders picks. No same-team penalty.

### V10.1 Lineup Composition: 1P + 4B with Mini-Stack Only

Exactly 1 pitcher + 4 batters. The pitcher anchors Slot 1 (2.0×).

**Dual cap system** (both must be satisfied):

| Cap | Value | When |
|---|---|---|
| `MAX_PLAYERS_PER_TEAM_BATTERS_STACKABLE` | 2 | team is in a stack-eligible game |
| `MAX_PLAYERS_PER_TEAM_BATTERS_DEFAULT` | 1 | every other team |
| `MAX_PLAYERS_PER_GAME_BATTERS` | 2 | always — per-game hard cap |

Structural constraints:
- `REQUIRED_PITCHERS_IN_LINEUP = 1` (exactly 1 pitcher per lineup)
- **Stacking gate**: see `is_stack_eligible_game()` in `app/core/constants.py`
- **Never an opposing batter in the anchor's game** — pitcher ↔ hitter negative correlation
- Slot 1 = pitcher anchor, Slots 2-5 = batters by filter_ev descending

The active optimizer produces **two lineups** from the same FADE-excluded pool via `run_dual_filter_strategy`.

### EV Formula (V10.6 — env/trait + V10.6 multipliers, single source: `_compute_base_ev()`)
```
filter_ev = env_factor                           # PRIMARY: pitchers 0.70–1.20, batters 0.70–1.30 (V10.6 asymmetric)
  × volatility_amplifier                         # 1 + cv × 0.20 × (env − 0.5) × 2 — V10.6 env-conditional, batters only
  × trait_factor                                 # SECONDARY: 0.85–1.15 (1.35× swing)
  × stack_bonus (1.20 if PATH 1 blowout favorite team, else 1.0)
  × dnp_adj (V10.6: unknown=0.93, confirmed_bad=0.70, known_order=1.0)
  × pitcher_pop_penalty (V10.5: 0.85 if pitcher AND FADE, else 1.0)
  × 100
```

**What each term uses (pre-game data only):**
- `env_factor` (batter): Vegas O/U, opposing starter ERA, opposing starter WHIP, **opposing starter K/9 (V10.6)**, bullpen ERA, platoon advantage, batting order (graduated 1-9, top-of-order = premium), park factor, weather (wind/temp), moneyline, series context, L10 form. Caps at 1.30.
- `env_factor` (pitcher): own K/9, opposing OPS, opposing K%, park, ML favorite, home field. Caps at 1.20 (V10.6 — pitcher 1-player dependence makes the saturation case dominate batters at 1.30).
- `trait_factor` (pitcher): FB velocity, induced vertical break, extension, whiff %, chase % (Statcast kinematics — 70% weight); K/9 (30% weight). Falls back to K/9-only when Savant has no row yet (new call-ups).
- `trait_factor` (batter): avg exit velocity (8 pts), hard-hit % (7), barrel % (6), max EV (2), HR/PA (2) — normalised over the POWER_PROFILE_DENOM of 25. Missing sub-signals drop out of both numerator and denominator so rookies with partial Savant coverage aren't penalised.
- `volatility_amplifier`: V10.6 amplifies env signal in good matchups, penalises in bad. Pitchers always 1.0 (no recent_form_cv).
- `pitcher_pop_penalty`: V10.5 — FADE-classified pitchers pay 15%; FADE batters never reach EV (excluded by `_exclude_fade_players`).

card_boost and draft counts **are not present on `FilteredCandidate` at all**. The optimizer is structurally incapable of reading them. The router joins them onto the response from the source `FilterCard` for display only.

Post-EV composition (applied in `_enforce_composition`):
- **Phase 0:** compute `stack_eligible_teams` from `slate_class.stackable_games` (favored team in a game that cleared BOTH the moneyline AND Vegas-total gates).
- **Pitcher anchor (Phase 1):** highest-EV pitcher selected, pinned to Slot 1. Only opposing batters in his game are blocked from batter picks.
- **Batter fill (Phase 2):** top-4 batters by filter_ev. Teams in `stack_eligible_teams` may contribute up to 2 batters (mini-stack); every other team is capped at 1. Independent per-game cap of 2 prevents mixed-side clumps. Stacking therefore fires only on overwhelmingly clear game scripts AND is size-limited to mini-stacks.

### Pre-V10 Strategy Evolution (V2.2–V9.0, condensed)

Earlier versions are superseded by the V10.x architecture above; the changelog narrative is preserved here in compact form for future-Claude context. **Active semantics live in code + constants.py + V10.x sections; nothing below is load-bearing.**

- **V2.2–V3.4 (Apr 6–12):** explored graduated penalties, Bayesian DEAD_CAPITAL floors, percentile ownership tiers, dynamic pitcher cap (1/2/3 based on boosted-pool richness), three-tier lineup construction, correlation bonuses, draft-scarcity tiebreakers, and pitcher-specific FADE moderation. April 11 post-mortem (Suarez/Sheehan/Bassitt chalk+3x sweeping ghost+max_boost) showed the dynamic pitcher cap was insufficient — the boost-tier × pitcher-pool × spot-bias interaction needed structural redesign.
- **V5.0 (Apr 13 — pitcher-anchor):** locked composition at 1 SP + 4 batters with pitcher pinned to Slot 1 (2.0×). Retired `compute_dynamic_pitcher_cap()` and the Slot 1 Differentiator contrarian swap. Constants `REQUIRED_PITCHERS_IN_LINEUP = 1` and `PITCHER_ANCHOR_SLOT = 1` come from here.
- **V8.0 (Apr 14 — signal hierarchy):** introduced env/trait/popularity modifier bands. `ENV_MODIFIER 0.70–1.30`, `TRAIT_MODIFIER 0.85–1.15` (still active). Pitcher moneyline added to env. Batter env restructured into Groups A (run env, capped) / B (situation) / C (venue). Batting order replaced top-5 cliff with graduated scale (still active).
- **V8.1 (Apr 15 — environment enrichment + production hardening):** added bullpen ERA via new `get_team_pitching_stats`; series/H2H context (Group D ±0.8); Vegas lines as a *required* enrichment (later promoted to fatal-on-failure under "Vegas Lines: Required, Never Optional"). Cache restart guard so post-T-65 dyno restarts call `lineup_cache.restore_and_refreeze()` instead of regenerating. Module-level loggers + `NON_PLAYING_GAME_STATUSES` extracted to constants.
- **V9.0 (Apr 16 — FADE gate replaces popularity multiplier):** retired `pop_factor` as an EV multiplier. FADE players are now *excluded* from the candidate pool before EV is computed (`_exclude_fade_players`); TARGET and NEUTRAL flow through identically. Removed `RS_CONDITION_MATRIX` and `condition_classifier`'s outcome-observation logic.

The V10 sections above describe the current architecture in full.

### Lineup Construction (V10.5 — EV-Driven Composition + Game-Script-Gated Mini-Stack)
Each lineup is either **1 SP + 4 batters** or **0 SP + 5 batters** — the EV-driven chooser in `_enforce_composition` (and `_build_best_variant` inside `run_dual_filter_strategy`) builds both variants and returns the higher slot-weighted total. Tiebreak goes to the pitcher variant (conservative default; preserves V5.0 pitcher-anchor identity unless 5B truly dominates). Stacking gates and per-team / per-game caps apply identically to both variants.

1. **Stack-eligibility (Phase 0)**: compute the set of teams whose game satisfies both gates, from `slate_class.stackable_games`.
2. **Pitcher anchor (Phase 1, anchored variant only)**: pick the highest-EV pitcher (by pre-game EV: Statcast FB velo/IVB/extension/whiff/chase + opponent K%/OPS + park + home + moneyline). Pin to Slot 1 (`PITCHER_ANCHOR_SLOT = 1`, 2.0× multiplier). Block **only opposing batters in his game** (pitcher ↔ hitter negative correlation). Teammates of the anchor are allowed (within caps).
3. **Fill batter slots (Phase 2)**: anchored variant fills 4 batter slots; pure-batter variant fills 5. Both sort by filter_ev descending; per-team batter cap is `MAX_PLAYERS_PER_TEAM_BATTERS_STACKABLE` (2) for stack-eligible teams, `MAX_PLAYERS_PER_TEAM_BATTERS_DEFAULT` (1) for all others; independent per-game cap is `MAX_PLAYERS_PER_GAME_BATTERS` (2).
4. **EV chooser (Phase 3)**: `_lineup_total_ev` computes slot-weighted total for each variant; the higher one is returned. If neither variant assembles 5 candidates (degenerate pool), `_enforce_composition` raises `ValueError` — no fallback.

### Lineup Validation (V10.5)
- **Per-team batter cap varies** — 2 for stack-eligible teams, 1 for all others
- **Per-game batter cap always 2** — prevents mixed-side 4-batter clumps
- **No opposing batter in the anchor's game** — anti-correlation guard (only applies in anchored variant; pure-batter variant has no such restriction)
- **At most 1 pitcher** (`REQUIRED_PITCHERS_IN_LINEUP = 1` is now interpreted as the *upper bound*) — `_validate_lineup_structure` warns if `pitcher_count > 1`. 0 (pure-batter) and 1 (anchored) are both legal.

### Slate Classification (informational only — does NOT force composition)
- Classification exists for blowout detection and display only
- **No slate type forces pitcher/hitter counts.**

**Moonshot** — V10.1: draws from the same FADE-excluded pool as Starting 5. Player overlap is allowed.
- Same structural shape: **1 SP anchor in Slot 1 + 4 batters in Slots 2–5** with the same conditional stacking gate
- Only the anchor's opposing game-side is blocked from batter picks
- **No contrarian multipliers** — FADE players excluded at the gate, not penalised in EV
- Sharp signal bonus: up to +35% EV from underground analyst buzz (Reddit, FanGraphs, Prospects Live)
- Explosive bonus: up to +20% EV from power_profile (batters) or k_rate (pitchers)
- Natural formula divergence (sharp × explosive re-ranks candidates differently from env × trait alone)

**Key functions (filter_strategy.py):**
- `run_filter_strategy()` — Starting 5 (V10.1: env/trait EV, pitcher-anchor, gated stacking)
- `run_dual_filter_strategy()` — One call, two lineups from same FADE-excluded pool
- `_exclude_fade_players()` — Hard gate: removes FADE candidates before EV, raises ValueError if no pitchers remain
- `_compute_base_ev()` — Shared formula: env × trait × context × 100
- `_compute_filter_ev()` — Starting 5 EV (delegates to `_compute_base_ev`)
- `_compute_moonshot_filter_ev()` — Moonshot EV (delegates to `_compute_base_ev` + sharp/explosive bonuses)
- `_compute_dnp_adjustment()` — Bifurcated DNP risk (V10.6: unknown=0.93, confirmed_bad=0.70)
- `_compute_stack_eligible_teams()` — Returns the set of team abbreviations whose game clears the moneyline AND Vegas-total stacking gate
- `_team_batter_cap()` — Returns the per-team batter cap (4 for stack-eligible, 1 otherwise)
- `_enforce_composition()` — Phase 1 picks highest-EV pitcher; Phase 2 fills 4 batters with conditional per-team caps. Raises `ValueError` if pool has no pitcher.
- `_validate_lineup_structure()` — Enforces per-team batter cap using `stack_eligible_teams`; blocks opposing batters in anchor's game; final pitcher-count assertion.
- `_smart_slot_assignment()` — Pitcher → Slot 1; batters → Slots 2-5 by filter_ev descending.

**Key functions (services/candidate_resolver.py):**
- `resolve_candidates()` — Builds candidate pool from DB, scores env + traits (Statcast when available), fetches web-scraped popularity (no platform ownership sources)

**Key functions (core/statcast.py):**
- `get_batter_kinematics(mlb_id, season)` — avg/max exit velocity, hard-hit %, barrel % from Baseball Savant
- `get_pitcher_kinematics(mlb_id, season)` — FB velocity, induced vertical break, extension, whiff %, chase % from Baseball Savant

## API Structure (6 routers under `/api/`)

| Router | Prefix | Purpose |
|---|---|---|
| filter-strategy | `/api/filter-strategy` | PRIMARY: single-lineup optimization (V11.0) |
| players | `/api/players` | Player CRUD + search |
| slates | `/api/slates` | Slate management + draft cards + results |
| scoring | `/api/score` | On-demand scoring + rankings (intrinsic scores only — no card_boost) |
| calibration | `/api/calibration` | Scoring weight configuration |
| pipeline | `/api/pipeline` | Orchestrated fetch → score → rank |

## Core Rules & Business Logic

1. **Sport-Specific:** This is MLB only. Do NOT add NBA/NFL/etc. logic.
2. **No fallbacks ever.** See "ABSOLUTE RULE" section above. If the pipeline fails, raise an error — never silently serve stale data.
3. **total_value is absolute:** Always `real_score * (2 + card_boost)`. Never null. Computed only via `compute_total_value()` in `app/core/utils.py`.
4. **card_boost is during-draft only.** It must NEVER appear as an input to the scoring engine, EV formula, or any pre-game prediction. V10.0 removed `card_boost` and `drafts` from `FilteredCandidate` entirely — the router reads them from the source `FilterCard` for the response payload. `card_boost` now exists only in: (a) `compute_total_value()` for historical CSV data, (b) DB storage models, (c) request/response schemas. The scoring engine and optimizer are structurally incapable of consuming it.
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

**Filter 4 — Individual Explosive Traits (TERTIARY, V10.0 Statcast-driven)**
- Batter power upside: avg exit velocity ≥ 92 mph, hard-hit % ≥ 50%, barrel % ≥ 15%, max EV ≥ 112 mph → elevated `power_profile` score (strategy doc §"Offensive Engine" — distance multiplier).
- Pitcher K upside: FB velocity ≥ 97 mph, induced vertical break ≥ 17 in, extension ≥ 6.5 ft, whiff % ≥ 32%, chase % ≥ 35% → elevated `k_rate` score (strategy doc §"Induced Vertical Break").
- Speed upside: SB pace ≥ 30/season → elevated `speed_component` score.
- These flow through the trait_factor (secondary signal, 0.85–1.15) — they break ties within the same pop+env tier.

**Filter 5 — Slot Sequencing (V10.5 EV-driven shape + gated mini-stack)**
- **Slot 1 (2.0×) is the highest-EV candidate** — the pitcher in the anchored variant, the highest-EV batter in the pure-batter variant.
- Remaining slots are batters, ordered by filter_ev descending. Per-team cap is 2 for stack-eligible teams (moneyline ≤ −200 AND O/U ≥ 9.0), otherwise 1. Per-game cap is always 2.

### EV-Driven Composition (V10.5): 0P+5B or 1P+4B, Mini-Stacks Only When Overwhelmingly Clear
Each lineup is either 1 pitcher + 4 batters or 0 pitcher + 5 batters — `_enforce_composition` builds both variants and returns the higher slot-weighted total EV. The pitcher (when present) is the best-condition pre-game SP. Batters are the highest-EV picks honouring the dual caps — at most 2 from any one team (only on stack-eligible game scripts), at most 2 from any one game, 1 per team otherwise. Opposing batters in the anchor's game are never drafted (anchored variant only).

## Deployment

- **Dockerfile** + **Procfile** included for Railway
- Environment vars use `BO_` prefix (see `.env.example`)
- SQLite by default, swap `BO_DATABASE_URL` for Postgres in production
- Database seeds automatically on startup via FastAPI lifespan
- Startup does **zero** pipeline work — the T-65 slate monitor is the sole pipeline trigger
- If the T-65 pipeline fails, the app returns HTTP 503 from `/api/filter-strategy/optimize` — this is correct behavior
