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

The active optimization path is `filter_strategy` — **not** `draft_optimizer.py` (which only powers the `/api/draft/evaluate` user-proposed-lineup endpoint).

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

Current coverage (as of 2026-04-18): **25 slates, 2026-03-25 → 2026-04-18**. All four files stay in lockstep — every date present in one is present in all four.

**Two roles — do not confuse them:**
- **Outcome labels** (`historical_players.csv`, `historical_winning_drafts.csv`) — retrospective results. What players scored, who won, what lineups paid off. Never used as live pipeline inputs.
- **Calibration ground truth** (`hv_player_game_stats.csv`, `historical_slate_results.json`) — what the conditions and player performances actually were. Used to validate that the live scoring signals correlate with real outcomes.

| File | Role | Current size | Contents |
|---|---|---|---|
| `historical_players.csv` | Outcome labels | 904 rows / 25 dates | Master player ledger: real_score, card_boost, drafts, leaderboard flags (HV/MP/3X). **Null `real_score` / `total_value` = DNP/scratch.** Avg ~36 rows/date (range 22–56). |
| `historical_winning_drafts.csv` | Outcome labels | 910 rows / 25 dates | Top-ranked lineups per date (5 rows per lineup). 4–12 ranks captured per date; target is 20. |
| `historical_slate_results.json` | Calibration ground truth | 25 entries | Per-date game context: game results and scores. Game objects can be extended with env condition fields (Vegas lines, ERA/K9/WHIP/hand, team OPS/K%, bullpen ERA, series context, weather) when needed for manual calibration analysis. Cross-reference with historical_players.csv by (date, team). |
| `hv_player_game_stats.csv` | Calibration ground truth | 396 rows / 25 dates | Actual box scores for every Highest-Value player appearance. Batting (ab, r, h, hr, rbi, bb, so) and pitching (ip, er, k_pitching, decision) coexist — blanks = not applicable. |

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

The CSV/JSON files are the source of truth; the SQLite DB is rebuilt from them via `app/seed.py`. `run_seed()` is **idempotency-guarded** — it only seeds if the `weight_history` table is empty (guard at `app/seed.py:257`). To pick up freshly appended rows:

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

**V9.0 integration:** Popularity is a **candidate pool gate**. FADE players (high pre-game media attention) are excluded from the candidate pool before EV computation begins. TARGET and NEUTRAL players pass the gate and are scored identically — no popularity multiplier in EV. The EV formula is driven purely by env (game conditions) and trait (season stats). DFS platform ownership was never pre-game knowable and is fully excluded.

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

> **Note:** V8.0 was superseded by V9.0 (April 16), which replaced the pop_factor EV multiplier with a hard FADE-exclusion gate. See the V9.0 section above for the current architecture. Production fixes in V8.0 changes 2–6 remain in effect.

**Design change:** The signal hierarchy is inverted based on 20-date empirical analysis. FADE/TARGET/NEUTRAL used as primary EV driver (3.0× swing). ENV_MODIFIER set to 0.70–1.30 (secondary). TRAIT_MODIFIER set to 0.85–1.15 (tertiary). V9.0 later replaced the popularity multiplier with a hard exclusion gate; env/trait now fully drive EV.

**Six changes:**

1. **Signal hierarchy inversion** (`app/core/constants.py`) — `ENV_MODIFIER` set to 0.70–1.30. `TRAIT_MODIFIER` set to 0.85–1.15. V8.0 used `POP_MODIFIER` 0.50–1.50 as the primary signal; V9.0 replaced this with the FADE-exclusion gate (`_exclude_fade_players()`), removing pop_factor from EV entirely.

2. **Pitcher moneyline added** (`app/services/filter_strategy.py`) — `compute_pitcher_env_score()` now accepts `team_moneyline`. Win bonus probability is a major pitcher RS component; heavy favorites (-250+) get full credit. Graduated from -110 (0) to -250 (1.0). `max_score` raised from 5.5 to 6.0.

3. **Batter env correlated-signal cap** (`app/services/filter_strategy.py`) — `compute_batter_env_score()` restructured into three signal groups. Group A (run environment: O/U, opposing ERA, moneyline, bullpen) **capped at 2.0** to prevent 4 correlated signals from inflating env score. Group B (player situation: platoon, batting order) up to 2.0. Group C (venue: park + weather) up to 1.0. `max_score` reduced from 7.5 to 5.5.

4. **Batting order graduated** — Hard top-5 gate replaced with graduated scale: order 1-3 → 1.0, 4-5 → 0.75, 6-7 → 0.50, 8-9 → 0.25. **Unknown batting order contributes 0 to the env situation group** — no mathematical guessing of a baseline. Missing-data risk is handled separately by `_compute_dnp_adjustment()`: ≥3 unknown env factors → DNP_UNKNOWN_PENALTY (0.85, data not published); <3 unknown → DNP_RISK_PENALTY (0.70, lineup published without player). This keeps env scoring faithful to actual pre-game signals while isolating DNP risk to a single multiplier — avoiding a double-penalty on ghost players whose orders are simply unpublished.

5. **All thresholds graduated** — Binary thresholds replaced with linear interpolation across both pitcher and batter env functions. Examples: K/9 from 6.0 (0) to 10.0 (1.0); opposing OPS from 0.780 (0) to 0.650 (1.0); park factor from 1.05 (0) to 0.90 (1.0). Eliminates false-precision cliffs on early-season sample sizes.

6. **Strategy documentation updated** — Filter 2 (now "Popularity / Crowd-Avoidance") formalized as the primary filter with empirical basis. Filter 3 (now "Environmental Advantage") demoted to secondary with correlated-signal grouping documented.

**New constants:**
- `POP_MODIFIER_FLOOR = 0.50`, `POP_MODIFIER_CEILING = 1.50` (was 0.85/1.15)
- `ENV_MODIFIER_FLOOR = 0.70`, `ENV_MODIFIER_CEILING = 1.30` (was 0.50/1.50)
- `TRAIT_MODIFIER_FLOOR = 0.85`, `TRAIT_MODIFIER_CEILING = 1.15` (was 0.70/1.30)

### V8.1 Changes (April 15 — Series Context, Bullpen ERA, Vegas Lines, Cache Restart Guard)

> **Note:** V8.1 was superseded by V9.0 (April 16) for EV architecture. Fixes 1–4 and 6 remain in effect. Fix 5 (condition matrix observations) is no longer relevant — the condition classifier no longer stores RS observations.

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
- New `app/core/odds_api.py` client. `BO_ODDS_API_KEY` env var; omitting it skips enrichment with a loud warning (env scoring treats NULL lines as unknown/neutral — existing behavior).
- `enrich_slate_game_vegas_lines()` populates `vegas_total`, `home_moneyline`, `away_moneyline`. Non-fatal in `run_full_pipeline()`. **Superseded — as of the "Vegas Lines: Required, Never Optional" policy, this call is now fatal. Any API failure or missing per-game odds raises `RuntimeError` and crashes the pipeline. The non-fatal note above applied to the initial V8.1 implementation only.**

**Fix 5 — Condition matrix** (retired in V9.0)
- `RS_CONDITION_MATRIX` and `RS_CONDITION_OBSERVATIONS` were removed in V9.0. `condition_classifier.py` now only exports `compute_draft_entropy()` and `compute_gini_coefficient()` for meta-game monitoring.

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
   - **Phase 2:** fill 4 batter slots from remaining candidates by `filter_ev` descending, blocking the anchor pitcher's `game_id`.
   - Team cap (1) and overall game cap (1) still apply.

4. **`_validate_lineup_structure()` rewritten** — new signature accepts `anchor_pitcher`.
   - Anchor pitcher is exempt from team/game cap checks.
   - Final sanity check asserts `pitcher_count_final == REQUIRED_PITCHERS_IN_LINEUP`.

5. **`_smart_slot_assignment()` rewritten** — the anchor pitcher is pinned to `PITCHER_ANCHOR_SLOT` (Slot 1). Batters are distributed across Slots 2–5 with unboosted batters getting the highest available slots (Slot 2 → Slot 5 tail for boosted). The Slot 1 Differentiator contrarian swap is gone — Slot 1 is reserved for the anchor pitcher in every lineup.

**Implications for prior strategy text:**
- Any CLAUDE.md / README statement that the optimizer may produce 0, 2, or 3 pitchers is **obsolete**. The count is now fixed at 1.
- The "unboosted players MUST go in Slot 1" guidance now applies only to batters — and only within Slots 2–5. Slot 1 is the pitcher anchor.
- The "ghost+boost batters outweigh a 2nd pitcher slot" reasoning is no longer a dynamic comparison; the structure is pre-committed.

### Historical Strategy Log (V2.2–V3.4, April 6–12)

Versions V2.2 through V3.4 explored graduated penalty mechanics (env/score), probabilistic ownership tiers, dynamic pitcher capping, and within-tier differentiation via condition matrices. Key research directions: (1) Bayesian smoothing replaced binary DEAD_CAPITAL floors (Beta-Binomial prior: 0/8 obs → 0.10 floor vs 0.0). (2) Bifurcated DNP handling (unknown=0.85, confirmed=0.70, ghost_unknown=0.92) accounted for data sparsity. (3) Percentile-based ownership tiers (empirical CDF) replaced absolute draft thresholds. (4) Dynamic pitcher caps flexed between 1–3 based on boosted-pool size and ghost tier presence. (5) Three-tier lineup construction (auto/soft_auto/rest), correlation bonuses (+10–20% EV on ghost teammates), and draft-scarcity tiebreakers (+10% EV for < 5 drafts) differentiated within ownership tiers. (6) Pitcher-specific FADE moderation (15% haircut vs 25% for batters) recognized that pitchers control their own outcomes. April 11 empirical analysis (Suarez/Sheehan/Bassitt chalk+3x dominance vs ghost+max_boost busts) revealed that dynamic pitcher capping was insufficient — the interaction between boost tier, pitcher pool richness, and playoff-style spot bias required structural redesign. All graduated-penalty functions (`_graduated_env_penalty()`, `_graduated_score_penalty()`, `_apply_ghost_boost_ev_floor()`) and condition-matrix-dependent logic (Bayesian floors, percentile tiers, three-tier fill order) are superseded by V9.0's simplified FADE-gate + env/trait EV architecture. These versions remain instructive for understanding how information asymmetry (crowd vs real outcomes) drove repeated pivots.

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
- `compute_draft_entropy(draft_counts)` — Shannon entropy for meta-game monitoring (observability only, not used in EV)
- `compute_gini_coefficient(draft_counts)` — Gini coefficient for meta-game monitoring (observability only, not used in EV)

**Key functions (services/candidate_resolver.py):**
- `resolve_candidates()` — Builds candidate pool from DB, scores env + traits, fetches web-scraped popularity (no platform ownership sources)

**Scope note:** `app/services/draft_optimizer.py` is used only by `/api/draft/evaluate` to score a user-proposed lineup and warn on suboptimal slot assignment. All automated lineup construction lives in `filter_strategy`.

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
- Environment vars use `BO_` prefix (see `.env.example`)
- SQLite by default, swap `BO_DATABASE_URL` for Postgres in production
- Database seeds automatically on startup via FastAPI lifespan
- Startup does **zero** pipeline work — the T-65 slate monitor is the sole pipeline trigger
- If the T-65 pipeline fails, the app returns HTTP 503 from `/api/filter-strategy/optimize` — this is correct behavior
