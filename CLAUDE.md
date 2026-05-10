# Ben Oracle - Rule-Based MLB Lineup Optimizer

## General Engineering Principles

### 1. Think Before Coding
**Don't assume. Don't hide confusion. Surface tradeoffs.**
- State assumptions explicitly.
- If multiple interpretations exist, present them — don't pick silently.

### 2. Goal-Driven Execution
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
- Return league-average / `0.5×max_pts` neutral baselines when a trait input is missing
- Use `or 0`, `or 1`, `or ""`, `.get(key, default)` in scoring or env paths to paper over `None`

The pipeline either works with real data or it fails loudly. Fallbacks mask bugs, corrupt optimization with wrong data, and violate the "Filter, Not Forecast" philosophy — you cannot filter on yesterday's environment.

### Strict-mode enforcement (May 2026)

The codebase was audited end-to-end for silent fallback paths and converted to fail-loud behavior. The five concrete enforcement layers:

1. **DNP filter (`app/core/utils.py::is_player_scoreable`)** — single source of truth for "scoreable from live data". Pitchers need IP > 0 plus ERA, WHIP, and K/9; batters need PA > 0 plus at least one Statcast power signal (avg_ev / hard_hit / barrel / x_woba / max_ev). Applied identically in `app/services/pipeline.py::run_score_slate` and `app/routers/filter_strategy.py::_load_active_slate`. Both call sites also drop batters whose `SlatePlayer.batting_order` is `None` (not in the RotoWire-projected lineup).  Rookie-track players (V13.2, see below) are admitted regardless of traditional-stat coverage and are scored separately.
2. **Trait scorers (`app/services/scoring_engine.py`)** — every previously-silent fallback (`UNKNOWN_SCORE_RATIO × max_pts`, `return 0`, `return max_pts * 0.4`) raises `RuntimeError`. The DNP filter ensures these raises are unreachable in normal operation; if one fires, it is a real data-collection bug, not a missing-data event.
3. **Env scorers (`app/services/filter_strategy.py::compute_pitcher_env_score`, `compute_batter_env_score`)** — top-of-function precondition checks raise on any `None` input. The previous `if x is not None: ...` skip-if-missing pattern is gone.
4. **`PARK_HR_FACTORS` lookups** — replaced `.get(team, 1.0)` with direct `[team]` indexing so an unknown team raises rather than silently scoring as a neutral park.
5. **Removed dead constants** — `DEFAULT_OPP_OPS`, `DEFAULT_OPP_K_PCT`, `DEFAULT_PITCHER_ERA`, `DEFAULT_PITCHER_WHIP`, `DEFAULT_BATTER_OPS_VS_LHP`, `DEFAULT_BATTER_OPS_VS_RHP`, `UNKNOWN_SCORE_RATIO`, `DNP_RISK_PENALTY`, `DNP_UNKNOWN_PENALTY`, `ENV_UNKNOWN_COUNT_THRESHOLD`. Don't reintroduce them.

### Rookie Scoring Track (V13.2 — May 2026)

Strict-mode enforcement above assumes that every player in the candidate pool has at least the minimum traditional stats (ERA/WHIP/K9 for pitchers, OPS for batters). For nearly every player in the league this is true — returning veterans, IL returnees, and mid-day acquisitions all carry stats from their current or prior MLB season, and `fetch_player_season_stats` falls back to prior-season aggregates when current-season is empty.

The one exception is **true MLB debutants**: a rookie spot-starter with zero career MLB IP, or a September call-up with their first plate appearance tonight. They have no current-season stats AND no prior-season stats AND no Statcast leaderboard row yet. Pre-V13.2, the strict assertions in `pipeline.run_fetch_player_stats` raised on these players and crashed the entire T-65 pipeline — one rookie spot-start would kill optimization for all 14 other games on the slate.

V13.2 fixes this with a separate scoring track:

- `PlayerStats.is_rookie_track` (Boolean) — set by `fetch_player_season_stats` when the player has zero current-season stats AND zero prior-season stats AND fewer than `ROOKIE_GAMES_THRESHOLD` (3) MLB games of experience for batters / less than `ROOKIE_PITCHER_IP_THRESHOLD` (5.0) career IP for pitchers.
- The strict assertions in `run_fetch_player_stats` skip rookie-track players with a logged warning. Non-rookies still raise — a 5-year veteran missing ERA is a real data-collection bug, not a missing-data event.
- `is_player_scoreable` admits rookie-track players regardless of stat coverage.
- `score_player` routes them to `score_rookie`, which returns the neutral score (`ROOKIE_NEUTRAL_SCORE = 57.5`) and an empty traits list. This calibrates `trait_factor` to exactly 1.0 in the EV formula, so the rookie's EV is purely env-driven (`env_factor × 1.0 × stack_bonus × dnp_adj × 100`).

This is *not* a fallback. The rookie has no traditional stats — period. Pretending they have league-average ERA would be a fallback (forbidden); routing them through a separate scorer that doesn't depend on traditional stats is a separate code path with its own admission rules. Empirically the crowd fades rookies and the optimizer should treat them as "decided by environment" until they cross the threshold and rejoin the traditional track. A rookie's Statcast leaderboard row populates after ~50 batted balls / pitches; once they cross `ROOKIE_GAMES_THRESHOLD` games their flag clears automatically on the next pipeline run and they're scored normally.

**Weather, Vegas, and team stats are NOT exceptions to strict mode.** Open-Meteo, The Odds API, and the MLB Stats API team endpoints serve every team and every game on every slate; a missing wind/temp/moneyline is a vendor outage or app misconfiguration, never a "true rookie" equivalent. These still raise loudly and crash the pipeline so ops investigates and restores the upstream source. There is no slate-level "drop the game" path — the only legitimate missing-data case is the rookie-track carve-out above.

### Live data resiliency (May 2026)

The strict raises stay theoretical only because the data layer fetches reliably. Every external client uses `httpx` + `tenacity` with full-jitter exponential backoff (AWS-recommended for thundering-herd-safe retries) and split connect/read/write/pool timeouts:

| Client | File | Attempts | Backoff | Connect | Read |
|---|---|---|---|---|---|
| MLB Stats API | `app/core/mlb_api.py` | 4 | random_exponential, max=8s | 4s | 15s |
| The Odds API | `app/core/odds_api.py` | 4 | random_exponential, max=8s | 4s | 15s |
| Open-Meteo | `app/core/open_meteo.py` | 4 | random_exponential, max=6s | 4s | 10s |
| RotoWire | `app/core/rotowire.py` | 5 | random_exponential, max=8s | 5s | 20s |
| Baseball Savant (pybaseball) | bulk via `pybaseball` (`scripts/refresh_statcast.py`) | n/a | n/a | n/a | n/a |
| Baseball Savant (raw scrapes) | `app/core/statcast.py::_savant_get` | 4 | random_exponential, max=8s | 4s | 15s |

Concurrency is capped at 20 simultaneous MLB API requests (`asyncio.Semaphore`) so the T-65 ~260-request burst doesn't trigger rate limits. RotoWire treats `200 OK` with zero parsed games as a transient CDN-warmup failure and retries (the only HTML scrape on the path; HTML can be partial without a status code change). Every client follows redirects (`follow_redirects=True`) so a vendor moving an endpoint behind a 301 doesn't crash the pipeline.

If all retries exhaust, the client raises and the T-65 pipeline crashes loud — `/optimize` returns HTTP 503 with the underlying exception. There is no fallback layer. The fix is always to restore the upstream service or deploy a corrected client; never to add a default value.

## Vegas Lines: Required, Never Optional

**Critical Requirement:** Vegas lines (moneyline + over/under totals) are **mandatory inputs** to the T-65 pipeline. The Odds API (`BO_ODDS_API_KEY` environment variable) must be configured and operational.

**Behavior:**
- `BO_ODDS_API_KEY` **must be set** at startup. If missing, the app logs a critical warning at initialization.
- When the T-65 pipeline runs, `enrich_slate_game_vegas_lines()` **raises `RuntimeError`** if:
  - The API key is unset
  - The API request fails (network error, timeout)
  - The API response indicates an error (401 invalid key, 422 quota exhausted, etc.)
  - **NO** game on the slate has matching odds (full vendor outage)
- Users see HTTP 503 with the specific error message when they try to fetch picks.

**Per-game tolerance for partial coverage (May 2026 mid-slate redeploy fix):** within a single Odds API response, individual games can be missing — typical causes include bookmakers pulling lines on weather risk (Coors Field is the most common offender), doubleheader splits where only one game has been priced, late scheduling changes, or sportsbook-specific gaps. Pre-fix, a single missing game crashed the entire slate (zero picks generated). Post-fix, when the API is reachable but odds are missing for a SUBSET of games, those games are dropped from the slate (their `SlateGame` + `SlatePlayer` rows deleted) and the pipeline continues with the remaining games. Per-game drops log a loud `WARNING` so ops sees the coverage gap.

**This is NOT a fallback in the no-fallback-policy sense.** We never substitute fake moneylines or default totals. We strictly shrink the slate to the games we can score on real data — exactly the same posture as the existing `is_game_remaining` filter that drops already-started games on a mid-slate cold start. The full-outage case (zero matched games) still raises loudly. The downstream `MIN_GAMES_REPRESENTED` guard in `run_full_pipeline` catches the case where too many games dropped to produce a meaningful slate.

**Why this matters:** mid-slate redeploys (Railway dyno cycles after the day's first pitch) routinely run the pipeline 30-60 minutes from imminent first pitches, where bookmakers may have pulled lines for one or two games due to weather or late-breaking news. Crashing the entire slate over one game would produce zero picks for the user when 14 of 15 games are perfectly scoreable.

**Why Vegas lines matter:** they feed directly into pitcher and batter environmental scoring:
- **Pitcher env (Factor 5):** Moneyline determines win-bonus probability (heavy favorite -250+ gets full credit).
- **Batter env (Group A, A1/A3):** Vegas O/U (over/under) and moneyline determine run-scoring environment.

A game with NULL moneyline cannot be scored — that's why dropping it is correct, not papering it over with defaults.

**Configuration:** Set `BO_ODDS_API_KEY` to your The Odds API key (free tier: 500 requests/month, sufficient for one pipeline run per day).

## ABSOLUTE RULE: Historical Data Is Reference Only

**Historical stats from CSV/DB must NEVER be used as a direct input feature, normalization anchor, or baseline weight in the live daily pipeline.**

This means:
- `total_value` and leaderboard flags from `historical_players.csv` are **never** EV inputs — they're retrospective outcome labels only. (`card_boost` and `drafts` were dropped from the historical CSVs entirely; they're not relevant to pipeline calibration.)
- Past slate real scores and total values cannot feed forward into prediction or scoring.
- If a scoring baseline is needed, derive it from archetypal expectations (league-average defaults in `constants.py`) or conditional variables (pre-game conditions), not past performance.

**What IS permitted:**
- `PlayerStats` (ERA, WHIP, K/9, OPS, etc.) fetched from the live MLB Stats API — these are factual season aggregates.
- `PlayerGameLog` records for recent form — populated by `fetch_player_season_stats()` from the live MLB API. Historical CSV game logs (`hv_player_game_stats.csv`) are a supplementary seed only; the live API is authoritative.
- `historical_players.csv` for building the initial `Player` table (name, team, position, MLB ID) — identifying data, not predictive inputs.

**Why?** Using historical RS or leaderboard outcomes as predictive inputs creates data leakage — you'd be learning from outcomes that weren't knowable before the draft. The condition matrix in earlier versions (`RS_CONDITION_MATRIX`) was removed in V9.0 for exactly this reason.

**Calibration scripts are permitted.** Scripts in `/scripts/` may read historical outcome columns (`real_score`, `total_value`, `is_highest_value`, `is_most_popular`, `is_most_drafted_3x`, `drafts`) for calibration, threshold tuning, and analysis purposes. The hard rule is the runtime separation: `app/` (the live pipeline) must never read these columns. The CI gate `scripts/audit_live_isolation.py` enforces this isolation — banned outcome fields are scanned in `app/` only, and `scripts/` is exempt. Calibration outputs inform constant edits in `app/core/constants.py` and `app/core/weights.py` by hand.

### What historical data IS for: calibration

The historical files exist to answer one question: **do the live signals we score on actually correlate with real outcomes?**

The live pipeline consumes two categories of signal at T-65:
1. **Player performance signals** — season stats (ERA, K/9, OPS, recent form) from the MLB Stats API
2. **Game context signals** — Vegas lines, weather, bullpen ERA, series context, etc.

The historical data captures outcomes of those same conditions:
- `hv_player_game_stats.csv` — what the top players actually did (box scores). Ground truth on player performance.
- `historical_slate_results.json` (game objects, including env fields once populated) — what the game context actually looked like. Ground truth on game conditions.
- `historical_players.csv` — real_score and HV/MP/3X flags. Outcome labels to measure prediction quality against.

The calibration loop runs two ways: (a) manual — Claude reads the CSVs directly and edits `app/core/constants.py` / `app/core/weights.py` when a threshold is clearly miscalibrated; (b) scripted — calibration scripts in `/scripts/` may read outcome columns (`real_score`, HV flags, `drafts`, `total_value`) and produce threshold-tuning analysis. Either way the edits to constants are reviewed by hand before commit. The hard rule remains: `app/` (the live pipeline) never reads outcome columns.

## Architecture Overview

### Active Pipeline

The active (and only) optimization path is `filter_strategy`.

**Four-Stage Pipeline:**
1. **Collect** (`app/services/data_collection.py`) — Fetch MLB schedule + boxscores + player stats
2. **Score** (`app/services/scoring_engine.py`) — Rate players 0-100 via trait profiles
3. **Filter** (`app/services/filter_strategy.py`) — Score env (Vegas O/U, opp ERA/WHIP/K9, bullpen, park, weather, platoon, batting order, ML, series, L10, opp rest days)
4. **Optimize** (`app/routers/filter_strategy.py` → `run_filter_strategy`) — Produce a single lineup (1P+4B or 0P+5B chosen by total EV)

### Philosophy
**RS is a latent target variable.** The Real Sports scoring formula is proprietary and opaque — we can observe leaderboard outcomes (HV, MP, 3X flags, real_score) but not the formula that generated them. This forces a **proxy modeling approach**: rank players by observable pre-game conditions that correlate with HV outcomes, and treat high EV as a proxy for RS upside.

Rule-based scoring + external-variables filtering (NOT ML). The rule-based architecture is deliberate: a fitted statistical model would optimize toward historical RS, creating the data-leakage feedback loop the no-historical-bleed rule prevents. Calibration is manual — Claude reads historical data and edits `app/core/constants.py` directly. Interpretable by design, structurally prevented from leaking outcome data into prediction.

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
     - Populate Player + SlatePlayer rows by fetching each team's active roster from the MLB Stats API team endpoint (the DB is empty until this point — there is no historical seed)
     - **RotoWire expected lineups** (hard dependency) — populates `batting_order` from beat-reporter projections, sets `batting_order_source` to `"rotowire_confirmed"` or `"rotowire_expected"`. Failure raises `RuntimeError` and the pipeline crashes (HTTP 503 on `/optimize`). RotoWire is the de-facto industry source for expected lineups and the single source of truth at T-65 — MLB's official boxscore card only appears 30-60 min before each game's first pitch (usually after T-65 for all but the earliest game on the slate), so chasing it adds complexity without meaningful coverage.
     - Fetch season stats for all players
     - Enrich game environment (Vegas lines, series context, weather)
     - Bulk-load Statcast leaderboards from Baseball Savant (`scripts/refresh_statcast.py::main` invoked from `pipeline.py::_refresh_statcast`) — kinematics + xStats + team catcher framing onto `PlayerStats` / `TeamSeasonStats`
     - Score all players (0-100 trait profiles)
     - Run filter strategy (single lineup, V12.x)
   - **No fallbacks.** If any stage fails — including RotoWire, Vegas, weather, Statcast — the monitor crashes so `/optimize` returns HTTP 503.
   - Freeze cache with `lineup_cache.freeze()` — picks are now immutable

4. **Post-Lock Monitoring** (After T-65):
   - Picks are available immediately once the T-65 pipeline completes and Redis is written
   - `/api/filter-strategy/optimize` serves frozen picks (zero computation per request)
   - Lightweight 60-second loop monitors game completion
   - On all-final, **purge** cache (memory + Redis + SQLite) and pre-warm tomorrow's pipeline.  Purge — not clear — so the Redis 24h TTL doesn't keep yesterday's frozen payload alive past the slate's actual end (a dyno crash between slate-complete and midnight could otherwise let `restore_and_refreeze` re-freeze the finished slate's picks).

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

If a Redis-frozen T-65 payload from earlier in the day exists, the `restore_and_refreeze` path in `app/main.py` brings those picks back unchanged — but only when the cached payload's `deploy_id` matches the currently running process's deploy ID.

**Dev-redeploy cache busting (May 2026).** Each freeze stamps the meta key with `_current_deploy_id()` (Railway's `RAILWAY_DEPLOYMENT_ID`, falling back to `RAILWAY_GIT_COMMIT_SHA`, then `local-dev`). On startup, `restore_and_refreeze` compares the cached deploy_id to the running one:
- **Same deploy_id** (dyno crashed and restarted on the same image) → restore frozen picks unchanged. Picks must not churn on a transient crash.
- **Different deploy_id** (a code push has shipped, or a manual Railway redeploy) → return False so the caller `purge()`s all three cache tiers (memory + Redis + SQLite) and the monitor runs a cold pipeline on remaining games. Every dev redeploy mid-slate gets fresh picks computed from the still-upcoming games only.

This is the explicit knob to bust cache: push a commit (new SHA) or hit "Redeploy" in Railway (new deployment ID). Restoring after a crash on the same deploy keeps producing the same picks — no fallback, just the right semantics for both intents.

**Restart restore extends to the full T-65 lock window (May 2026).** Earlier the startup-time restore guard only fired when `now >= first_pitch`. A same-deploy crash *between* lock and first pitch (e.g. 30 min after lock) was purging the cache and re-running the pipeline, which could churn picks despite the deploy_id being unchanged. The guard now restores whenever `now >= lock_time` — `restore_and_refreeze` returns False on cache miss (pre-lock crash) or new deploy_id (code push), and the caller still purges in those cases. Net effect: transient same-deploy crashes ANYWHERE post-lock leave picks frozen; new deploy_id ANYWHERE post-lock busts cache + recomputes on remaining games.

One source of truth: `STARTED_GAME_STATUSES = frozenset({"Live", "Final"})` and `is_game_remaining(game_status)` live in `app/core/constants.py`.

**Recent-stats fallback for probable starters with no current-season IP (May 2026).** The strict assertion at the end of stage 2 (`run_fetch_player_stats`) requires every announced starter to have ERA/WHIP/K9 — without those numbers, every batter's matchup env scoring crashes downstream. Three live edge cases routinely hit this: pitchers returning from the IL (Strider 2026-05-03), season-debut starters (Trey Gibson), or players acquired mid-day who haven't pitched yet for their new team. Their 2026 IP is 0, so `fetch_player_season_stats` previously left ERA/WHIP/K9 None.

Per the user directive ("live data with full context and recent player performance"), `fetch_player_season_stats` now falls back to **prior-season pitching aggregates** for any pitcher with IP=0 in the current season. Strider's 2025 ERA is a real, factual signal — using it when his 2026 IP is 0 is "most recent actual performance", not a league-average default. The fallback is gated to position ∈ {P, SP, RP} and only fires when current-season IP is empty. True rookies with no prior-season IP either still trip the strict assertion (no fallback to defaults — pipeline crashes loud) so the gap is named in the log and a human can investigate, or the DNP filter excludes them on the batter side via `is_player_scoreable`. Not a "fallback default", which is forbidden — it's the player's own previous-season real data, used only when the current season is genuinely empty.

**Post-lock state-aware monitoring (May 2026).** The post-lock loop previously polled `/schedule` every 60 seconds for the entire 4-5h slate window — ~250 wasted API calls per slate, since no game can be Final before its own first pitch. The loop is now bounded by the slate's known timing:

1. `_get_last_first_pitch_utc()` returns the latest scheduled first pitch on the slate.
2. The monitor sleeps until `last_first_pitch + MIN_GAME_DURATION_MINUTES` (150 min) before any polling — this is the earliest moment ANY game on the slate can plausibly be Final. Zero MLB API calls during this window.
3. After that, polls `/schedule` every `POST_LOCK_CHECK_INTERVAL` (120 s) until every game has final scores or sits in NON_PLAYING_GAME_STATUSES.

Net effect: a 15-game slate with first pitches spanning 17:35–22:05 ET produces ≤30 schedule fetches in the post-lock window (down from ~250). Picks stay frozen the entire time. The monitor re-emerges from Phase 4a with no compute cost.

**Post-lock cache purge on slate completion (May 2026).** When the post-lock loop detects all games final, it now calls `lineup_cache.purge()` rather than `lineup_cache.clear()`. `clear()` only nulls the in-memory state; the Redis `lineup:{today}` and `lineup:meta:{today}` keys keep their 24h TTL.  If the dyno crashed in the narrow window between "slate complete" and "midnight UTC", the next boot's `restore_and_refreeze` would see a same-deploy_id meta entry with `slate_date == today` and re-freeze the just-finished slate's picks — the user wouldn't see tomorrow's lineup until midnight rolled the active slate date forward.  `purge()` wipes all three tiers (memory + Redis + SQLite) so a post-completion crash always falls through to the fresh next-slate cycle.

**The four runtime windows the monitor handles uniformly:**
1. **Normal T-65 trigger (cold morning start)**: Phase 1 bootstrap → Phase 2 sleeps until T-65 → Phase 3 full pipeline → Phase 4 state-aware completion watch.
2. **Restart in `now < lock_time`**: startup restore is a no-op (no cache yet); monitor runs the normal sleep-then-pipeline path.
3. **Restart in `lock_time <= now < first_pitch` (T-65 window)**: same-deploy → `restore_and_refreeze` brings frozen picks back, monitor skips Phase 3 and proceeds to Phase 4. New deploy → cache busted, monitor runs cold pipeline (no games started yet, so all 15 are still in scope).
4. **Restart in `first_pitch <= now < last_first_pitch + MIN_GAME_DURATION` (mid-slate)**: same-deploy restores; new deploy runs pipeline filtered to `is_game_remaining` games. Phase 4 sleeps as much as it can before polling.

**Key Functions:**

- `app.services.slate_monitor.targeted_slate_monitor()` — Main T-65 event loop
- `app.services.slate_monitor._get_first_pitch_utc()` — Parse game times, compute lock time
- `app.services.slate_monitor._sleep_until()` — Chunked async sleep for responsive cancellation
- `app.services.lineup_cache.freeze()` — Freeze picks after T-65 run
- `app.core.utils.is_pipeline_callable_now()` — Gate manual pipeline endpoints
- `app.core.constants.is_game_remaining()` — Shared filter for started-game exclusion

## Data Files (`/data/`)

Current coverage (as of 2026-05-10): **43 slates, 2026-03-25 → 2026-05-07**.

**Canonical store: `data/historical.db`** (10 SQLite tables — see schema in `app/core/historical_db.py::SCHEMA_DDL`).  The 5 CSV/JSON files in `/data/` are derived exports refreshed automatically by every writer (the daily scraper, the Step-3 backfill scripts, the Step-9-18 external backfills, and the May 2026 cleanup-and-add Tier 1-3 backfills).  Calibration / audit scripts read through the SQLite store; the CSVs remain on disk for ad-hoc inspection.

### May 2026 cleanup-and-add sweep

Three concurrent changes against the post-Step-18 baseline:

1. **Cleanup — pure derivations dropped** from `slate_game` (winner / loser / winner_score / loser_score / home_team_runs / away_team_runs / home_team_run_differential / away_team_run_differential / home_team_winning_pct / away_team_winning_pct) and from `label_event` (label_types `total_value`, `avg_draft_slot`, `avg_draft_mult`, `avg_draft_tv`, `highest_draft_tv`, `most_common_slot`).  All recomputable from siblings already on the row.  `winner` etc. are still emitted into `historical_slate_results.json` on export — derived inline from `home_team`/`away_team`/`home_score`/`away_score`.  `total_value` recomputable from `real_score × (2 + card_boost)` — calibration scripts that read it now compute inline.
2. **Cleanup — weak / actionably-zero signals dropped** from `slate_game` (`weather_condition`, `game_duration_minutes`, `innings_played`, `home_mound_visits_used` / `away_mound_visits_used` / `home_abs_challenges_*` / `away_abs_challenges_*`, `ump_1b_id` / `ump_2b_id` / `ump_3b_id`) and from `player_slate` (`jersey_number`).  All zero readers; signal-cost ratio too low to justify storage.  `scripts/backfill_game_meta.py` deleted (all six of its outputs were dropped).
3. **Phase C — slowly-changing dimensions lifted to `player_dim`.** 8 attributes (bat_side / pitch_hand / birth_date / birth_country / mlb_debut_date / height_in / weight_lb / primary_position_code) moved from per-(slate_date, mlb_id) snapshot on `player_slate` to per-mlb_id `player_dim` (1644 × 8 = 13,152 cells → 399 × 8 = 3,192 cells, ~75% storage reduction).  `scripts/audit_player_dim_drift.py` documents the drift-audit path for future cache refreshes.

CSV / test contracts updated to match.  The full `pytest tests/` suite (275 tests) passes; `scripts/audit_live_isolation.py` clean; `scripts/validate_ingest.py --date 2026-05-07` passes.  Live pipeline (`app/`) untouched — this is purely historical-store hygiene plus expansion.

### Tier 1-3 external-data expansion (May 2026)

13 new external signals across three priority tiers — see `scripts/backfill_*.py` for the per-source backfill scripts.  All idempotent + per-source cached.  4 of 13 ship verified end-to-end on the existing corpus (D3 / D6 / D7 + the cleanup-side schema migration).  The remaining 9 are functional scripts behind external HTTP / paid API surfaces; they write 0 rows and log a warning when the source is unreachable.  Schema is in place either way.

**SQLite tables** (10 total: 6 from the migration, 1 from Phase C, 3 from Tier 1-3):

| Table | Rows | Cols | Role |
|---|---|---|---|
| `slate` | 43 | 7 | One per slate envelope: date, game_count, num_brawlers, season_stage, source, saved_at. |
| `slate_game` | 551 | **146** | Per-game env signals + post-game outcomes.  PK `(slate_date, game_pk, game_number)`.  Down ~14 from the post-Step-18 peak of 160 after the May 2026 cleanup (10 dropped derivations from Phase A + 12 dropped weak signals from Phase B = 22 dropped, partially offset by 8 added: Tier 1 D4 opening line snapshot — 4 cols + Tier 2 D7 rolling bullpen pitch counts — 4 cols).  Same shape as before for venue static, HP umpire only, catcher, attendance, day/night, full per-team box-score line, per-starter pitching detail, bullpen aggregate, Open-Meteo actual hourly weather, as-of-date team standings. |
| `player_slate` | 1644 | **69** | Per-(slate_date, mlb_id) identity + at-slate inputs.  Net +9 from the post-Step-18 peak of 60: Phase B dropped jersey_number + Phase C lifted 8 slowly-changing dims to `player_dim` (-9 cols), but Tier 1 D2 (per-catcher framing — 2 cols), Tier 1 D3 (pitcher_rest_days — 1 col), Tier 2 D5 (plate-discipline — 5 cols), Tier 2 D6 (BABIP / HR-FB regression — 4 cols), Tier 2 D8 (rolling-window handedness splits — 2 cols), Tier 2 D9 (DFS-site projected ownership — 2 cols), Tier 3 D10 (vendor projected fantasy points — 2 cols) added 18.  Same shape as before for OPS / ERA / WHIP / K9 / Statcast kinematics + xStats + season-aggregate platoon splits + batting_order_at_slate + arsenal_*_pct + sprint / OAA / bat-tracking. |
| `player_game_log` | 12290 | 21 | Prior-game outcomes per (slate_date, mlb_id, game_date) for the trailing 10-game window. |
| `label_event` | 12577 | 7 | Typed/sourced/dated outcome labels.  `label_type` ∈ {real_score, card_boost, drafts, draft_count, injury_status, highest_value, most_popular, most_drafted_3x, winning_lineup_slot, box_score, **wpa**}.  May 2026 cleanup dropped `total_value` + 5 draft-shape aggregates (all recomputable on export); Tier 3 D11 added `wpa` (Win-Probability-Added per HV game from `scripts/backfill_wpa.py`). |
| `player_alias` | 0 | 5 | Side table for HV box-score identity recovery; populated only on demand. |
| `player_dim` | 399 | 12 | **Phase C add (May 2026):** per-mlb_id slowly-changing dimensions.  Replaces 8 per-slate columns formerly on `player_slate`.  Populated by `scripts/backfill_player_externals.py`. |
| `umpire_dim` | 0 | 9 | **Tier 1 D1 add:** HP umpire historical K%/BB% tendencies per (ump_id, season).  Populated by `scripts/backfill_umpire_tendencies.py` from Umpire Scorecards. |
| `statcast_pa` | 0 | 11 | **Tier 3 D12 add:** per-batted-ball Statcast detail (exit velo, launch angle, x_woba, pitch type, result) for HV games by default.  Populated by `scripts/backfill_statcast_pa.py` from pybaseball. |
| `batter_pitch_type_woba` | 0 | 6 | **Tier 3 D13 add:** per-batter, per-pitch-type wOBA crosstab.  Populated by `scripts/backfill_batter_pitch_type_splits.py` from Savant per-pitch-type leaderboards. |

**Derived exports** (refreshed on every write to `data/historical.db`):

| File | Role | Current size | Source query |
|---|---|---|---|
| `historical_players.csv` | Outcome labels + at-slate features (34 cols) | 1644 rows / 43 dates | `player_slate LEFT JOIN label_event` for `real_score` / 3 boolean flags / `card_boost` / `drafts` / `draft_count` / `injury_status`.  May 2026 cleanup dropped `total_value` + 5 draft-shape aggregates (recomputable from real_score × (2 + card_boost) and the per-lineup `winning_lineup_slot` rows respectively). |
| `historical_winning_drafts.csv` | Outcome labels | 2308 rows / 43 dates | `label_event WHERE label_type='winning_lineup_slot'` (CSV row index encoded in `source` to preserve exact-duplicate rows). |
| `historical_slate_results.json` | Calibration ground truth | 43 entries / 551 games | `slate JOIN slate_game` (column `home_team` exported as `home`, `away_team` as `away`; `game_number` dropped from output).  `winner` / `loser` / `winner_score` / `loser_score` derived inline on export from `home_team` / `away_team` / `home_score` / `away_score`. |
| `hv_player_game_stats.csv` | Calibration ground truth | 751 rows / 43 dates | `label_event WHERE label_type='box_score' JOIN player_slate` for identity columns (box-score values stored as JSON in `label_event.label_text`). |
| `historical_player_game_logs.csv` | Calibration ground truth | 12290 rows / 41 dates | `player_game_log` ordered by `rowid_seq` (insertion order). |

**External-data backfill scripts (Steps 9-18, May 2026)** — each pulls from a single public source, writes to SQLite directly, caches per-source under `scripts/output/.*_cache/`, and is idempotent (re-run → cache hit → no-op):

| Script | Source | Schema target |
|---|---|---|
| `backfill_game_externals.py` | MLB Stats API gumbo `/api/v1.1/game/{pk}/feed/live` | slate_game venue / HP umpire / catcher / attendance / day_night |
| `backfill_pitcher_boxscore.py` | same gumbo cache | slate_game per-starter pitch_count / outs_recorded / hits/R/ER/BB/SO/HR + bullpen aggregate |
| `backfill_team_boxscore.py` | same gumbo cache | slate_game per-team hits/HR/SO/BB/LOB/SB/errors |
| `backfill_player_externals.py` | MLB Stats API `/api/v1/people?personIds=` (batched 50/req) | **player_dim** bat_side / pitch_hand / birth_date / mlb_debut_date / physicals / position |
| `backfill_pitcher_arsenal.py` | Savant `statcast_pitcher_pitch_arsenal(arsenal_type='n_')` | player_slate arsenal_*_pct + arsenal_dominant_pitch (joins to player_dim for position filter) |
| `backfill_sprint_oaa.py` | Savant `statcast_sprint_speed` + `outs_above_average` CSV | player_slate sprint_speed_fps / hp_to_first_sec / OAA / fielding_runs_prevented |
| `backfill_bat_tracking.py` | Savant bat-tracking leaderboard CSV | player_slate avg_bat_speed_mph / hard_swing_rate / swing_length_ft / squared_up / blast / swords |
| `backfill_weather_actuals.py` | Open-Meteo `archive-api.open-meteo.com/v1/archive` | slate_game actual_temperature_f / wind / humidity / precip / pressure / cloud |
| `backfill_standings.py` | MLB Stats API `/api/v1/standings?date=Y-M-D&hydrate=team` | slate_game per-team games_back / runs_scored / runs_allowed / streak / rank / home/away record |

**Tier 1-3 backfill scripts (May 2026 cleanup-and-add sweep)** — same idempotent + cached posture:

| Script | Tier | Source | Schema target |
|---|---|---|---|
| `backfill_umpire_tendencies.py` | 1 D1 | Umpire Scorecards `/api/umpire/{id}` | new `umpire_dim` table |
| `backfill_catcher_framing.py` | 1 D2 | Savant catcher-framing leaderboard CSV | player_slate framing_runs / framing_strike_rate (catchers only) |
| `backfill_pitcher_rest.py` | 1 D3 | derived from `player_game_log` (no external call) | player_slate pitcher_rest_days |
| `backfill_vegas_line_movement.py` | 1 D4 | The Odds API `/v4/historical/sports/baseball_mlb/odds` (paid tier) | slate_game opening_total / opening_*_moneyline / line_open_at |
| `backfill_plate_discipline.py` | 2 D5 | Savant batter expected-statistics CSV | player_slate bb_pct / k_pct / o_swing_pct / z_contact_pct / sw_str_pct |
| `backfill_regression_flags.py` | 2 D6 | derived from `player_game_log` | player_slate babip_at_slate / babip_regression_flag |
| `backfill_bullpen_rest.py` | 2 D7 | derived from `slate_game.{home,away}_bullpen_pitch_count` | slate_game home/away_bullpen_2d_pitches / 3d_pitches |
| `backfill_recent_handedness_splits.py` | 2 D8 | pybaseball `statcast_batter` filtered by p_throws | player_slate ops_vs_lhp_last_20 / ops_vs_rhp_last_20 |
| `backfill_dfs_ownership.py` | 2 D9 | FantasyPros MLB ownership scrape | player_slate dfs_projected_ownership_pct / dfs_projection_source |
| `backfill_vendor_projections.py` | 3 D10 | FantasyPros DK projections scrape | player_slate vendor_projected_points / vendor_projection_source |
| `backfill_wpa.py` | 3 D11 | pybaseball statcast_batter `delta_home_win_exp` per HV game | label_event `wpa` |
| `backfill_statcast_pa.py` | 3 D12 | pybaseball `statcast_batter` per (mlb_id, game_date) | new `statcast_pa` table (HV-only by default; `--all-games` for full corpus) |
| `backfill_batter_pitch_type_splits.py` | 3 D13 | Savant per-pitch-type batter splits | new `batter_pitch_type_woba` table |

**Limitations on point-in-time fidelity** — explicitly named so a future calibration sweep doesn't conflate them with bugs:
- `ops_vs_lhp_at_slate` / `ops_vs_rhp_at_slate` — MLB API does not honour `endDate` on `stats=statSplits`.  We backfill the season-total split (a single value per player, repeated across every slate the player appears on).  Approximation is exact for the most recent slates and slightly stale for late-March games.
- `batting_order_at_slate` — actual lineup-card slot from the post-hoc MLB box score, NOT the RotoWire pre-game projection the live pipeline reads at T-65.  Pinch-hit / double-switch / late-scratch cases are baked in.  HV-leaderboard players who entered as pinch-hits get a blank slot (correct: they didn't start).
- Statcast pitcher columns (`fb_velo`, `fb_ivb`, `fb_extension`, `whiff_pct`, `chase_pct`) — derived from pybaseball pitch-by-pitch with a 30-pitch minimum threshold.  Pitchers below the threshold leave columns blank rather than emit noisy values.
- Savant arsenal / sprint / OAA / bat-tracking columns — populated from the season-aggregate leaderboard at the time of the backfill, NOT a per-slate snapshot.  All slates for a given player carry the same value within a single backfill run.  Re-run mid-season to refresh.
- Standings snapshot at slate_date is exact-as-of-date (the `/standings?date=` endpoint).  Opening-week games (~3% of corpus) have NULL standings columns because no W-L existed yet.

## Env Scoring Calibration

The env scoring thresholds in `app/core/constants.py` (BATTER_ENV_VEGAS_FLOOR, ERA floors/ceilings, etc.) are tuned via two paths: manual reasoning and calibration scripts.

**Calibration scripts are permitted in `/scripts/`.** Scripts may read historical outcome columns (`real_score`, `total_value`, `is_highest_value`, `is_most_popular`, `is_most_drafted_3x`, `drafts`) and produce threshold recommendations, condition analyses, bucket audits, and any other quantitative output intended to inform scoring parameters. The output is reviewed by hand and edits to `app/core/constants.py` / `app/core/weights.py` are made deliberately — calibration scripts do not auto-apply changes. The hard separation is: `app/` (the live pipeline) never reads outcome columns. Calibration analysis lives in `scripts/`.

**How calibration is done:** Either by Claude reading `historical_players.csv` and `historical_slate_results.json` directly and reasoning about correlations, or by writing a calibration script in `/scripts/` that joins outcome columns against the live signals and reports per-bucket HV-rates / mean RS. The question is the same: *when the live pipeline would have rated a condition favorably, did players in those conditions actually score well?* If a threshold is clearly miscalibrated, edit `app/core/constants.py` directly.

**Other permitted scripts in `/scripts/`:**
- `backfill_slate_results_and_hv_stats.py` — fetches from the MLB Stats API to fill missing box scores and game results in `/data/`.
- Calibration / audit / threshold-recommendation scripts — read `/data/` outcome columns, write analysis to stdout or files in `/tmp/` or `/scripts/output/`. Never touch `app/`.

**CI gate:** `scripts/audit_live_isolation.py` — static grep-based scan of `app/` for banned outcome fields (`real_score`, `total_value`, `is_highest_value`, `is_most_popular`, `is_most_drafted_3x`) in runtime code paths. The scan covers `app/` only; `scripts/` is exempt by design. Run before every deploy.

## Ingesting New Slate Data

The default daily ingest is **automated via `scripts/scrape_realsports_daily.py`** — a Playwright-driven scrape against the Real Sports app that captures the day's leaderboards (HV/MP/3X), top-20 winning lineups, and game results from the platform's internal JSON endpoints. It writes to `data/historical.db` first, then refreshes the 5 derived CSV/JSON files via `scripts/export_historical_csvs.export_all()`. Idempotent (re-run with `--force` to overwrite a date already present).

Daily workflow (after a slate completes):

```
# 1. Real Sports leaderboard scrape (writes SQLite + refreshes CSVs/JSON)
.venv-scraper/bin/python scripts/scrape_realsports_daily.py             # yesterday in EST
.venv-scraper/bin/python scripts/scrape_realsports_daily.py --date YYYY-MM-DD

# 2. Step-3 backfills (pre-Step-9; populate at-slate stats + slate-game env)
.venv/bin/python scripts/backfill_slate_env_conditions.py               # Vegas/weather/pitchers per game
.venv/bin/python scripts/backfill_slate_results_and_hv_stats.py         # box scores from MLB API
.venv/bin/python scripts/backfill_player_season_stats_at_slate.py       # OPS/ERA/WHIP/K9 at the time of the slate

# 3. Step-9-18 external-data backfills (~135 columns, idempotent + cached)
.venv/bin/python scripts/backfill_game_externals.py                     # venue / umpire / catcher / attendance
.venv/bin/python scripts/backfill_pitcher_boxscore.py                   # per-starter pitch count + outs + hits/R/ER/BB/SO/HR
.venv/bin/python scripts/backfill_team_boxscore.py                      # per-team hits/HR/SO/BB/LOB/SB/errors
.venv/bin/python scripts/backfill_game_meta.py                          # mound visits + ABS challenges
.venv/bin/python scripts/backfill_player_externals.py                   # handedness / birth / debut / physicals
.venv/bin/python scripts/backfill_pitcher_arsenal.py                    # pitch arsenal % per pitcher
.venv/bin/python scripts/backfill_sprint_oaa.py                         # sprint speed + OAA per batter
.venv/bin/python scripts/backfill_bat_tracking.py                       # bat-tracking metrics per batter
.venv/bin/python scripts/backfill_weather_actuals.py                    # actual hourly weather (Open-Meteo Archive)
.venv/bin/python scripts/backfill_standings.py                          # GB / run differential / streak / rank

# 4. Verify
.venv/bin/python scripts/validate_ingest.py --date YYYY-MM-DD           # lockstep + duplicate check
.venv/bin/python scripts/audit_historical_corpus.py                     # full health report
```

Step-3 backfills end with `app.core.historical_db.rebuild_from_csvs_and_export()` (CSV-first → DB rebuild from CSVs → re-export).  Step-9-18 backfills write SQLite directly and call `export_historical_csvs.export_all()` (DB-first → CSV refresh) — guarded by a `HISTORICAL_DB` env-var check so audit reproducibility runs against a tmp DB don't clobber `/data/`.

The scraper requires `scraper/storage_state.json` (Playwright auth state). If it returns 401 or zero parseable games, refresh the auth state once interactively:

```
BO_REALSPORTS_PASSWORD=… .venv-scraper/bin/python scripts/scrape_realsports_daily.py --refresh-auth
```

Auth tokens have indefinite-ish lifetime, so this should rarely be needed.

The four-file ingest semantics below still apply — the scraper just automates the row-appending. The legacy "manual screenshot capture" workflow is documented in §"Improved Ingest Process (V9.1)" for the case where the scraper is unavailable (e.g. platform layout change broke the selector path) or you're backfilling pre-scraper dates.

### Per-slate ingest checklist (manual fallback)

The daily scrape is fully automated.  This checklist applies only when the scraper is unavailable (platform layout broke the selector, auth state expired and you can't refresh, etc.) and you need to ingest a slate by hand.

Use the SQLite store directly via the build script + your manually-prepared CSVs:

1. Generate or hand-edit the relevant rows in the on-disk CSVs (`data/historical_players.csv`, `data/historical_winning_drafts.csv`, `data/historical_slate_results.json`, `data/hv_player_game_stats.csv`) using the column shapes documented in the `Derived exports` table above.
2. Run `python scripts/build_historical_db.py --rebuild` to re-ingest the CSVs into `data/historical.db`.
3. Run `python scripts/export_historical_csvs.py` to refresh the CSVs back from SQLite (canonicalises team strings, sort order, and number formatting).
4. Run `python scripts/validate_ingest.py --date YYYY-MM-DD` to confirm the slate is well-formed.

Required column shapes (matches the byte-stable export):
- `historical_players.csv`: 40 columns starting `date,player_name,team,position,real_score,total_value,is_highest_value,is_most_popular,is_most_drafted_3x,…`.  See `tests/test_invariants.py::TestExportColumnContract::EXPECTED_HEADERS` for the full list pinned in tests.
- `historical_winning_drafts.csv`: 10 columns: `date,winner_rank,slot_index,player_name,team,position,real_score,slot_mult,card_boost,total_mult`.  5 rows per lineup, slot_mult ∈ {2.0, 1.8, 1.6, 1.4, 1.2}.
- `historical_slate_results.json`: top-level array of envelopes; each envelope has `date, game_count, games[], num_brawlers, season_stage, source, saved_at`.  Per-game shape: `{home, away, home_score, away_score, winner, loser, winner_score, loser_score, …env-fields…, game_pk, datetime_utc}`.
- `hv_player_game_stats.csv`: 20 columns: `date,player_name,team_actual,position,real_score,game_result,ab,r,h,hr,rbi,bb,so,ip,er,k_pitching,decision,notes,ops_at_slate,iso_at_slate`.

### Reloading the historical SQLite store

`scripts/build_historical_db.py --rebuild` is the single command for re-ingesting CSVs/JSON into `data/historical.db`.  It is idempotent: re-running over the same input produces the same database (modulo timestamps in `observed_at` columns).  Backfill scripts call `app.core.historical_db.rebuild_from_csvs_and_export()` automatically after their CSV writes (Step 3 hook), so the DB stays in sync without manual intervention.

The live operational DB (the T-65 monitor's ephemeral SQLite at `db/baseball.db`) is a SEPARATE store with 9 tables; see "Database Models (9 tables)" below.  The two stores never share data — `data/historical.db` is the calibration corpus, the operational DB is current-cycle live state.

Old historical-into-DB seed paths (`app/seed.py`, the `draft_lineups` / `draft_slots` / `weight_history` tables) were removed.  The operational DB now stores **only current-cycle live state** — the T-65 monitor populates `slates` / `slate_games` / `slate_players` / `player_stats` / `player_game_log` / `team_season_stats` / `cached_lineups` / `player_scores` / `score_breakdowns` from live MLB API + Savant calls.

**Schema is built from SQLAlchemy models, not migrations.** Because the DB is ephemeral by design (Railway containers wipe SQLite on every restart) and stores only current-cycle state, alembic migrations were the wrong tool — there's no historical state to evolve. `app/database.py::init_db()` calls `Base.metadata.create_all(engine)` at startup and the schema is whatever the models say. The `alembic/` directory and `alembic.ini` were deleted. Schema changes require: edit the SQLAlchemy model, restart the container, the new schema is created on the fresh DB.

### Example rows

```
# historical_players.csv
2026-04-09,Aaron Judge,NYY,OF,-0.7,-3.01,0,1,0
2026-04-09,Mick Abel,BAL,P,4.6,23.0,0,1,1

# historical_winning_drafts.csv
2026-04-09,1,1,Mick Abel,BAL,P,4.6,2.0
2026-04-09,1,2,Seth Lugo,KC,P,4.1,1.8

# hv_player_game_stats.csv
2026-03-25,Austin Wells,NYY,C,1.2,SF 0 NYY 7,3.0,1.0,2.0,0.0,0.0,1.0,0.0,,,,,2-for-3 | vs SF (away)
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
   - Use the best-quality data source (HV > MP > 3X for real_score and total_value)
   - Set flags: `is_highest_value=1` if in HV list, `is_most_popular=1` if in MP, `is_most_drafted_3x=1` if in 3X
4. Output one row per unique (name, team) pair with combined flags

**Python example:**
```python
players = {}  # (name, team) -> {rs, total_value, is_hv, is_mp, is_3x}
for source, source_list in [("HV", hv_players), ("MP", mp_players), ("3X", _3x_players)]:
    for p in source_list:
        key = (p["name"], p["team"])
        if key not in players:
            players[key] = {**p, "is_hv": 0, "is_mp": 0, "is_3x": 0}
        if source == "HV":
            players[key]["is_hv"] = 1
        elif source == "MP":
            players[key]["is_mp"] = 1
        elif source == "3X":
            players[key]["is_3x"] = 1
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
output_rows.append((pitcher["name"], pitcher["team"], "P", 1, pitcher["rs"], slot_mults[0]))
for slot, batter in enumerate(batters, start=2):
    output_rows.append((batter["name"], batter["team"], "OF", slot, batter["rs"], slot_mults[slot - 1]))
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
- [ ] **Total value:** Platform-displayed post-boost value, blank if not captured
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

After appending rows, validate the corpus:

```bash
python scripts/validate_ingest.py --date 2026-04-17
```

This script checks:
- All three CSV files have the new date in lockstep (count match)
- No duplicate (date, player_name, team) in historical_players.csv
- All `slot_mult` values are in {2.0, 1.8, 1.6, 1.4, 1.2}
- Pitcher count in historical_winning_drafts.csv = 1 per lineup (4 per date)
- Flag counts are consistent (HV + MP + 3X ≤ total unique players)
- real_score in reasonable range (typically -5 to +10, warn on outliers)

**Exit codes:**
- 0: All checks passed, safe to reseed
- 1: Warnings (e.g., unusual values) — review before proceeding
- 2: Errors (e.g., duplicates, structural issues) — fix and rerun

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
2026-04-17,Tyler Glasnow,LAD,P,6.4,12.8,0,1,0
2026-04-17,Ranger Suarez,PHI,P,7.7,18.48,1,0,0
2026-04-17,Max Muncy,LAD,OF,5.6,28.0,1,0,0
```

**Step 3 — Build historical_winning_drafts.csv rows:**
- Rank 1 (oshavis): T. Glasnow (P) → Slot 1; remaining 4 by RS descending → Slots 2–5
- Rank 2–4: repeat structure
- Result: 4 × 5 = 20 rows

**Step 4 — Build hv_player_game_stats.csv rows:**
- For each of 14 HV players, match team to game
- Populate game_result; leave batting/pitching columns null (no image data)
- Result: 13 rows (1 player's team not in games list)

**Step 5 — Validate:**
```bash
python scripts/validate_ingest.py --date 2026-04-17
```
The DB does not store historical data; appending to the four /data files is the entire ingest.

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

These are the live operational DB tables, populated by the T-65 monitor from live MLB API + Savant calls.  The DB is ephemeral by design (Railway containers wipe SQLite on every restart).  The historical calibration corpus lives in a SEPARATE store at `data/historical.db` (5 tables — `slate / slate_game / player_slate / player_game_log / label_event`); see "Data Files (`/data/`)" above.  The two stores never share data.

| Model | Table | Key Fields |
|---|---|---|
| `Player` | players | name, name_normalized, team, position, mlb_id |
| `PlayerStats` | player_stats | Current-season aggregates (batting + pitching, Statcast kinematics, framing) |
| `PlayerGameLog` | player_game_log | Per-game records (H, HR, RBI, IP, K, ER, etc.) — populated by live MLB API |
| `TeamSeasonStats` | team_season_stats | Current-season team-level rollups (catcher framing) |
| `Slate` | slates | date, game_count, status — current-cycle only |
| `SlateGame` | slate_games | home_team, away_team, Vegas lines, starter ERA/K9, weather |
| `SlatePlayer` | slate_players | batting_order, env_score, source provenance |
| `PlayerScore` | player_scores | total_score (0-100) from the live scoring engine |
| `ScoreBreakdown` | score_breakdowns | Per-trait sub-scores |
| `CachedLineup` | cached_lineups | Frozen T-65 picks (one row per slate date) |

The operational DB stores **only current-cycle live state**.  Historical reference data lives exclusively in `data/historical.db` and its 5 derived export files; the only `app/` reader of the historical store is `app/core/popularity.py` (the rolling 14/28-day fame index, see scripts/audit_live_isolation.py for the carve-out).

## Scoring Engine (`app/services/scoring_engine.py`)

**Pitcher traits** (5 traits, 0-100): ace_status(25), k_rate(25), matchup_quality(20), recent_form(15), era_whip(15)

**Batter traits** (7 traits, 0-100): power_profile(25), matchup_quality(20), lineup_position(15), recent_form(15), ballpark_factor(10), hot_streak(10), speed_component(5)

`matchup_quality` (V10.6) is a four-sub-signal blend: opp ERA (35%), opp WHIP (20%), batter-vs-handedness OPS split (30%), and a K-vulnerability cross-penalty (15%). The K-vuln signal multiplies a normalised batter K% (`so/pa`, floor 18% / ceiling 30%) by a normalised opp K/9 (floor 7.5 / ceiling 11.0); only the (high × high) corner fires the full penalty. This is the trait-layer answer to "0-for-4 with 3K" risk: a contact-oriented hitter is fine vs an elite K-arm because their bat-to-ball floor protects them, and a high-K hitter is fine vs a contact pitcher because the pitcher won't generate the whiffs to bury him — only the cross is dangerous. Anti-aligned with env Group A6 (opp K/9 alone): env scores the OPPORTUNITY for runs, trait scores the FLOOR for an individual batter. When sub-signals are absent (rookie batter no PA, missing K/9), the blend re-normalises across the available terms.

Weights are constants — change them in `app/core/weights.py` directly.  No runtime API, no DB persistence, no save/load: calibration is manual.

**card_boost must NEVER appear in the scoring engine.** It's revealed only during/after the draft and is structurally absent from `FilteredCandidate` — enforced by `tests/test_invariants.py` and `scripts/audit_live_isolation.py`.

**No league-average defaults** (May 2026 strict pass). The `DEFAULT_*` constants and `UNKNOWN_SCORE_RATIO` were removed — the trait scorers raise on any missing input. The DNP filter (`is_player_scoreable` + the batting-order check) excludes any player who would otherwise force a fallback. See "Strict no-fallback policy" below.

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

- **Scoring thresholds:** `SCORING_K9_FLOOR/CEILING`, `SCORING_ERA_CEILING/RANGE`, `SCORING_PITCHER_OPS_CEILING/RANGE`, etc.
- **Pitcher env thresholds:** `PITCHER_ENV_OPS_FLOOR/CEILING`, `PITCHER_ENV_K9_FLOOR/CEILING`, `PITCHER_ENV_ML_FLOOR/CEILING`, etc.
- **Batter env thresholds:** `BATTER_ENV_VEGAS_FLOOR/CEILING`, `BATTER_ENV_ERA_FLOOR/CEILING`, `BATTER_ENV_WIND_OUT_DIRECTIONS`, etc.
- **No `DEFAULT_*` / `UNKNOWN_SCORE_RATIO` / `DNP_*_PENALTY` constants** — removed in May 2026 strict pass. Missing live data raises; the DNP filter prevents that path.

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

**Strategy Version: V12.1 "Audit-Driven Env Scoring + Multi-Pitcher Variants"** — The optimizer was rebuilt from a 35-slate quartile audit of every pre-game signal against actual HV outcomes. Dead and inverted signals were deleted; strong signals were kept. Multi-pitcher variants (0P-5P) replaced the 0P/1P-only constraint that was structurally barring 57% of empirical winning shapes. Pitchers now cap at env_factor 1.40 (vs batter 1.30) — empirical pitcher RS is 34% higher than batter RS. ENV_MODIFIER_FLOOR dropped to 0.20 for signal discrimination.

**EV formula** (V12.1):
```
filter_ev = env_factor × volatility_amplifier × trait_factor × stack_bonus × dnp_adj × 100
  env_factor (pitcher): 0.20 → 1.40 (3.5× swing)
  env_factor (batter):  0.20 → 1.30 (3.25× swing)
  trait_factor:         0.85 → 1.15 (1.35× swing)
  stack_bonus:          1.0 / 1.20 (PATH 1 blowout-fav only)
  dnp_adj:              0.70 / 0.93 / 1.0
```

**Composition** (V12 multi-pitcher variant chooser):
The optimizer builds variants 0P+5B, 1P+4B, 2P+3B, 3P+2B, 4P+1B, 5P+0B. For each variant, picks top n_p pitchers by EV + top (5-n_p) batters by EV under per-team caps (1 default, 2 stack-eligible) AND anti-correlation guard (no opposing batters to any drafted pitcher unless they're the pitcher's teammate). The variant chooser slot-weights each candidate set by sorting all 5 by EV descending (rearrangement-inequality maximum) and assigning slot multipliers 2.0, 1.8, 1.6, 1.4, 1.2. Returns the highest-total variant.

**Final slot display: pitcher(s) always front (May 2026).** After the variant chooser picks the 5 players, `_smart_slot_assignment` reorders for display — pitchers fill slots 1..N in EV-desc order, then batters fill slots N+1..5 in EV-desc order. The selection of which 5 still uses rearrangement inequality (so EV ranking is unchanged); only the on-screen slot numbering shifts so the pitcher is always at the top of the lineup card.

**V12 env signals** (audit-validated, see V12 changelog below):
- Batter env: opp_starter_era (STRONGEST), opp_starter_whip, wind_speed_mph, park_hr_factor, team_moneyline (UNDERDOG premium — inverted from intuition), batting_order, temperature, platoon_advantage
- Pitcher env: team_moneyline (PEAK at mild fav -120 to -180 — also inverted from V11), vegas_total (INVERSE — low total = pitcher game), park_hr_factor, pitcher_k_per_9 (talent), own_starter_era (tail bonus/penalty), opp_team_ops (tail)

**Stacking** is still capped at 2 batters per team AND 2 per game and only fires on overwhelmingly clear game scripts.

### V16 Phase 2 — Trait sub-weight DRY refactor + corpus-saturation finding (May 8)

Phase 2 is **a no-behavior-change refactor + a calibration null result.**

**The refactor (real value, ships):**

Extracted every inline trait sub-weight to named constants in
`app/core/constants.py` so the audit harness can sweep them via
`BO_OVERRIDE_<NAME>` env vars, eliminating the previous
source-of-truth duplication that required parallel edits in
scoring_engine.py + the harness for every calibration sweep:

- `OFFENSIVE_PROFILE_OPS_WEIGHT`, `_X_WOBA_WEIGHT`, `_HARD_HIT_WEIGHT`,
  `_BARREL_WEIGHT`, `_AVG_EV_WEIGHT` (5 sub-weights inside batter
  offensive_profile, previously inline 10/7/5/4/4)
- `KINEMATIC_BLEND_KIN_WEIGHT`, `KINEMATIC_BLEND_K9_WEIGHT` (pitcher
  k_rate kinematic-vs-K9 blend, previously inline 0.70 / 0.30)
- `ERA_WHIP_ERA_WEIGHT`, `ERA_WHIP_WHIP_WEIGHT` (pitcher era_whip
  blend, previously inline 0.60 / 0.40)
- `PITCHER_WEIGHT_*` and `BATTER_WEIGHT_*` (outer trait weights;
  `app/core/weights.py` dataclass defaults now read from these)

`scoring_engine.py` reads the sub-weights via `from app.core import
constants` and dotted access (`constants.OFFENSIVE_PROFILE_OPS_WEIGHT`)
so monkey-patching at runtime is picked up correctly — the harness
patches the module attribute, the scorer sees the new value on the
next call.

`scripts/audit_hv_hit_rate.py::compute_trait_score_from_csv` was
rewritten to be a **faithful mirror** of `score_offensive_profile`,
`score_pitcher_k_rate`, and `score_pitcher_era_whip`:
- Reads the same threshold constants the live engine uses
  (`OFFENSIVE_PROFILE_OPS_FLOOR`, `POWER_PROFILE_X_WOBA_FLOOR`, etc.).
  Pre-Phase-2 the harness had its OWN `_TRAIT_*` constants that drifted
  from the live values — OPS floor 0.65 vs live 0.70, x_woba ceiling
  0.45 vs 0.40, hard_hit floor 25 vs 30, barrel floor 3 vs 4, avg_ev
  floor 86 vs 85, ceiling 95 vs 92.  Six of seven batter thresholds
  differed.  Those duplicates are gone.
- Uses the same outer trait weights (`PITCHER_WEIGHT_ACE_STATUS`,
  `PITCHER_WEIGHT_K_RATE`, `PITCHER_WEIGHT_ERA_WHIP`,
  `BATTER_WEIGHT_OFFENSIVE_PROFILE`).
- Composes pitcher trait by mirroring the engine's three pitcher
  scorers (ace_status + k_rate + era_whip — recent_form skipped
  because it needs game logs not in the CSV).

`SWEEPABLE_CONSTANTS` tuple in `audit_hv_hit_rate.py` is now the single
source of truth for which `BO_OVERRIDE_<NAME>` env vars take effect;
both `audit_hv_hit_rate.py` and `audit_lineup_tv.py` call
`apply_sweep_overrides()` from there (was duplicated as two local
override lists pre-Phase-2 — `audit_lineup_tv.py`'s local list missed
the trait-sub-weight entries, silently nullifying half of the early
sweep results until the dedup landed).

**Dead code deletion (DRY audit):**

- `POSITION_VOLUME_MULTIPLIER = {}` in `app/core/constants.py` deleted
  entirely (V16 Phase 1 had left it as an empty-dict import-stub; no
  app code imports it anymore).
- `BO_DROP_POSITION_VOLUME` env-var override path in both audit
  harnesses removed (was a no-op since the dict was empty).
- `position_mult` variable, CSV column, `position_haircut` miss-cause
  classification all removed from `audit_hv_hit_rate.py`; replaced
  with `trait_factor` exposure (the new actual lever).

**The calibration sweep — null result:**

Ran ~30 calibration configurations against the lineup-TV harness on
the now-43-slate corpus.  Surface area swept:

- Trait modifier band: [0.95, 1.05] / [0.90, 1.10] / [0.90, 1.15] /
  [0.85, 1.15] / [0.70, 1.20] / [0.70, 1.30]
- Batter sub-weights: OPS-heavy (18-6-3-2-1, 30-0-0-0-0), Statcast-
  heavy (5-12-5-4-4), xwOBA-paired (15-12-1-1-1), 2× / 3× OPS
- Pitcher sub-weights: kinematic blend 30/70 / 50/50 / 70/30 / 90/10;
  era_whip 50/50 / 60/40 / 70/30; ace_status / k_rate / era_whip
  outer weight reshuffles
- Leverage band: [0.75, 1.55] (V15.6) / [0.80, 1.30] (V16) / [0.85,
  1.30] / [0.90, 1.30] / [0.80, 1.35] / [0.85, 1.40]
- Popularity slope: 0.15 / 0.20 / 0.22 (V16) / 0.25 / 0.30
- Popularity neutral: 4.0 / 4.5 (V16) / 5.0
- Env band: floor 0.10 / 0.15 / 0.20 (V16) / 0.25 / 0.30; ceiling
  1.25 / 1.30 (V16) / 1.35
- Stack bonus: 1.00 / 1.05 / 1.10 (V16) / 1.15 / 1.20

**Best lineup TV mean across all configs: 81.6.  V16 baseline: 81.5.
Within noise.  No config produces a meaningful win.**

The structural reason: trait and leverage are anti-correlated on the
corpus.  High-trait players (OPS-Q4 batters, sub-3-ERA pitchers) are
disproportionately popular (high pop_score → leverage discount).
Low-trait players are disproportionately unpopular (low pop_score →
leverage premium).  A multiplicative formula `env × trait × leverage`
has those two terms cancelling each other — adding more trait signal
just shifts mass between two flavours of cancellation, doesn't
improve net ranking.

The user-validated within-sleeper finding still holds: among NOT-MP
batters (n=812), Q4 OPS hits HV at 90.5% vs Q1 64% (26.5pp swing).
But pulling that signal into the ranking via heavier OPS weighting
also pulls IN-MP players up via the same OPS, where leverage discount
nullifies the lift.  No clean way to extract the within-sleeper
gradient without conditioning on pop_score (which is a structural
formula change, not a constant tweak — Phase 3 candidate, gated on
out-of-sample validation that V16 holds first).

**What ships in V16 Phase 2:**

1. The DRY refactor (trait sub-weights extracted to constants;
   harness mirrors engine; SWEEPABLE_CONSTANTS single source of truth).
   Future calibration sweeps need only env vars, no code edits.
2. Dead-code deletion (POSITION_VOLUME_MULTIPLIER stub,
   BO_DROP_POSITION_VOLUME, position_mult column).
3. **No constant value changes.**  All weights stay at V16 Phase 1
   levels.  V16 Phase 1's lineup-TV mean 81.5 (vs rank-1 winning 78.2)
   is the corpus-saturation point.

**Verification**: 258/258 tests pass; ruff lint + format clean;
audit_live_isolation clean (the harness still reads outcome columns
in `/scripts/`-only paths, app stays clean).  Live runtime smoke-test
verified the constants-based sweep mechanism works end-to-end —
monkey-patching `constants.OFFENSIVE_PROFILE_OPS_WEIGHT` and
re-calling `score_offensive_profile` produces the expected
re-weighted score.

**V16 Phase 2 explicitly does NOT change:** any weight VALUES, V13 ML
curves, V13 wind-direction split, V13 catcher framing, V13.3 rookie
env ceiling, V13.3 STACK_BONUS, V15.4 trait band, V15.7 symmetric env
ceiling, V15.3 MAX_PITCHERS_PER_LINEUP cap, V12 composition,
per-team / per-game caps, anti-correlation guard, slot-display rule,
T-65 timing, no-fallbacks rule, no-historical-bleed rule.

### V16 Phase 1 — Lineup-TV calibration: leverage tightened, POSITION_VOLUME_MULTIPLIER removed (May 8)

V16 Phase 1 is the structural unification commit promised by V16
Phase 0 (commit 06bde49) Statcast backfill.  Two changes:

1. **POPULARITY_MULT_FLOOR / CEILING**: 0.75 / 1.55 → **0.80 / 1.30**
2. **POSITION_VOLUME_MULTIPLIER**: removed (V13.3 catcher 0.90 / 2B-SS
   0.95 haircut deleted)

Trigger: 2026-05-08 user complaint — the optimizer picked B. House
(WSH 3B, x_woba 0.32) over R. Jeffers (MIN C, x_woba 0.43) on the
same slate.  House busted; Jeffers would have hit.  Diagnosis showed
two independent miscalibrations: the V13.3 catcher haircut applied
a 10% population-level penalty to Jeffers as an individual elite-
trait catcher, AND the V15.6 leverage band [0.75, 1.55] amplified
House's contrarian premium beyond Jeffers' trait edge.

**The metric that drove V16: full lineup TV outcome, not per-player hit rate.**

`scripts/audit_lineup_tv.py` (new) replays the live runtime composition
(V15.3 1P-cap, per-team / per-game caps, anti-correlation guard) on
every slate in the 41-slate corpus, then sums the slot-weighted
total_value outcome.  The per-PLAYER TV-rate@K metric V15.6 was
tuned against ("did our top-K contain a top-TV winner?") rewards
aggressive contrarianism.  The per-LINEUP metric ("what's the SUM
across our 5 picks?") penalises the same aggressive contrarianism —
when 2-3 of our 5 picks are extreme contrarians from the same
low-pop_score slice, they bust together and tank the lineup.

**Calibration evidence (41-slate corpus, V15.7 → V16 Phase 1):**

| Metric                       | V15.7  | V16    | Δ      |
|------------------------------|--------|--------|--------|
| Lineup TV mean               |  78.6  |  81.7  | **+3.1** |
| Lineup TV median             |  80.6  |  85.8  | **+5.2** |
| Lineup TV p75                |  92.0  |  99.3  | **+7.3** |
| Lineup TV max                | 149.3  | 148.8  |  -0.5  |

Rank context: rank-1 winning lineup mean TV across the corpus is
**78.2**.  V15.7 mean (78.6) was tied with rank-1.  **V16 mean (81.7)
BEATS rank-1 by 3.5 TV/slate.**  p75 99.3 means 25% of slates produce
a rank-1-equivalent (~100 TV) lineup, up from very rare in V15.7.

**Leverage band sweep (the dominant lever):**

| Leverage band  | Lineup TV mean | Δ vs V15.7 |
|----------------|---------------:|-----------:|
| [0.75, 1.55] (V15.6) | 78.6 |  baseline  |
| [0.80, 1.45]   |  79.0  |  +0.4  |
| [0.85, 1.40]   |  80.0  |  +1.4  |
| [0.90, 1.35]   |  80.8  |  +2.2  |
| [0.85, 1.30]   |  82.0  |  +3.4  |
| **[0.80, 1.30]** (V16) | **81.9** | **+3.3** |
| [1.00, 1.30]   |  80.6  |  +2.0  |

Optimum at CEILING 1.30 (down from V15.6's 1.55).  FLOOR is in noise
between 0.80 and 0.95; 0.80 chosen to preserve a meaningful contrarian
discount magnitude.

**Why V15.6's wider band was wrong**: V15.6 calibrated against per-
PLAYER TV-rate@K.  A wide leverage band rewards the rare big payoff
of nailing a top-TV-winning sleeper as ONE of K picks.  But the same
band over-builds all-contrarian LINEUPS — the contrarians are picked
from a correlated slice of the player pool (low pop_score) and bust
together when none of them hit.  Per-LINEUP TV is the actual win
condition; per-player TV-rate is a proxy that disagrees with reality
at the lineup-construction level.

**Configurations tested but NOT shipped:**

- **Real Statcast-driven trait_factor (Phase 0 unblocked it)**: the
  harness measurement consistently HURT (real trait + any band <
  flat-trait baseline).  Reason: per fangraphs.com research, xwOBA
  is *descriptive not predictive* (R²=0.218 next-year-wOBA).  Live
  runtime already uses Statcast via `score_offensive_profile` and
  `score_pitcher_k_rate`; the live trait_factor is not flat 1.0.  The
  HARNESS's measurement was on a CSV-only proxy (skipping game-log
  derived recent_form / hot_streak that the live runtime computes).
  V16 trait band stays at V15.4's [0.70, 1.20].
- **Increasing stack cap (2 → 3 or 4)**: zero measurable effect.
  Stack-eligible games are rare AND individual EV ranking doesn't
  pull a 3rd batter from the stack into the lineup over a different
  team's higher-EV pick.  Stacking is correlation-driven, not
  individual-EV-driven; the cap relax doesn't help without
  correlation-aware composition (Phase 2 candidate).
- **Increasing STACK_BONUS** (1.10 → 1.20 / 1.25): -0.5 TV mean.
  Stack-eligible teams already hit the 2-cap; the bonus mostly
  affects single-batter-on-stack-eligible-team cases.
- **Wider leverage** (e.g. [0.65, 1.70]): -2.0 TV mean.  Confirms V16's
  tightening direction.

**Per-PLAYER metric regressions (acceptable):**

| Metric              | V15.7 | V16  |
|---------------------|------:|-----:|
| HV captured @5      |  160  | 156  |
| HV captured @10     |  303  | 281  |
| TV captured @5      |   58  |  54  |
| Slot-1 in top-5 TV  |   17  |  14  |
| Mean slot-1 TV      |  19.2 | 17.4 |

These are PROXY metrics — V15.6 calibrated for them, V16 trades them
for the actual win condition.  Net: +3.1 TV/slate on lineup TV vs
~3 fewer per-player hits.  Positive trade for "win the draft."

**Implementation surface:**

- `app/core/constants.py` — POPULARITY_MULT_FLOOR 0.75 → 0.80,
  POPULARITY_MULT_CEILING 1.55 → 1.30, POSITION_VOLUME_MULTIPLIER
  set to empty dict (kept for import-path compat; any `.get()`
  silently returns 1.0).
- `app/services/filter_strategy.py` — `_compute_base_ev` no longer
  computes `position_mult`.  EV formula collapses to
  `env × volatility × trait × leverage × stack × dnp × 100`.
- `scripts/audit_lineup_tv.py` (new) — the lineup-TV harness V16 was
  calibrated against.  Reuses `score_one_player` from
  `audit_hv_hit_rate.py`; constructs duck-typed candidates that feed
  `_build_variant` directly so the runtime composition logic is the
  one being measured.
- `scripts/audit_hv_hit_rate.py` — extended with Statcast-driven
  trait reconstruction (`compute_trait_score_from_csv`, opt-in via
  `V16_REAL_TRAIT=1`).  Default still flat for V15.7 parity.  Adds
  `BO_DROP_POSITION_VOLUME` toggle for sweeps.
- `tests/test_filter_strategy.py::TestV133PositionAndRookie` —
  rewritten.  Catcher EV now EQUALS OF EV at matched env+trait
  (no haircut).  Rookie env-cap tests preserved.

**V16 Phase 1 explicitly does NOT change**: V13 ML curves, V13 wind-
direction split, V13 catcher framing K-rate adjustment, V13.3 rookie
env ceiling (1.10), V13.3 STACK_BONUS=1.10, V15.4 trait band [0.70,
1.20], V15.7 symmetric env ceiling (pitcher / batter both 1.30),
V15.3 MAX_PITCHERS_PER_LINEUP=1 cap, V12 composition chooser,
per-team / per-game caps, anti-correlation guard, slot-display rule,
T-65 timing, no-fallbacks rule, no-historical-bleed rule.

**Verification**: 258/258 tests pass; ruff clean; audit_live_isolation
clean.  The lineup-TV harness is reproducible — `BO_CURRENT_SEASON=2026
.venv/bin/python scripts/audit_lineup_tv.py` re-runs the audit any
time the corpus grows.

**Phase 2 candidates (pending out-of-sample validation of V16)**:
correlation-aware composition (true 3+ batter stacks via joint upside
scoring), and slot-1 ceiling-probability term structurally distinct
from the multiplicative trait term.  Both are structural changes,
not constant tweaks; V16 first to confirm the [0.80, 1.30] band
holds up on the next ~10-15 slates.

### V15.7 Symmetric env ceiling — pitcher 1.55 → 1.30 (May 7, T-65 ship #2)

V15.7 reverts the V13 asymmetric env ceiling architecture after the
TV-target audit on the 42-slate corpus showed it was justified by the
wrong response variable (RS, not TV).

**The V13 hypothesis (under RS / HV-rate)**: pitchers carry a +38% RS
premium over batters (3.49 vs 2.47), so the env ceiling should be
asymmetric — pitchers cap at 1.55, batters at 1.30 — to express that
premium in the EV multiplier.  This was a clean win on RS-correlated
metrics under V13.

**The TV-target inversion**: pitcher mean TV is **-21% below batter
mean TV** (9.02 vs 11.41), even though pitcher mean RS is +41.5%
above batter mean RS (3.49 vs 2.47).  The reversal is structural:
pitchers concentrate in the high-fame / low-boost end of the
platform's pricing distribution (Skenes 5/6 RS=8.7 with boost=0,
Eovaldi 5/6 RS=7.4 with boost=0).  Confirmation: only 13.3% of slate
top-5-TV winners are pitchers across the corpus.

Under V15.6's asymmetric ceiling, pitchers were systematically
over-ranked in EV against their actual TV outcomes — a side effect
of calibrating against RS instead of TV.

**Sweep result** (audit_hv_hit_rate.py with TV metrics, V15.6
baseline):

| pitcher_ceiling | HV@5 | HV@10 | HV@20 | TV@5 | TV@10 | TV@20 | s1tv | s1MT |
|---|---|---|---|---|---|---|---|---|
| 1.55 (V15.6) | 158 | 298 | 503 | 58 | 189 | 574 | 17/42 | 17.87 |
| **1.30 (V15.7)** | **160** | **303** | **515** | **58** | **193** | **584** | **17/42** | **19.16** |
| 1.20 (aggressive) | 164 | 305 | 520 | 59 | 195 | 587 | 16/42 | 19.11 |

`PITCHER_ENV_MODIFIER_CEILING` 1.55 → 1.30 (now equal to
`ENV_MODIFIER_CEILING`) is a strict Pareto improvement over V15.6:
every metric same or better, slot-1 hit rate preserved, mean slot-1
TV outcome up **+1.29 (+7.2%)**.  Going further to 1.20 lifts HV@5
by another +4 but costs 1 slot-1 hit — V15.7 takes the
slot-1-preserving point.

**Architectural impact**: the asymmetric-ceiling architecture is
gone.  Pitchers and batters both saturate env at 1.30.  The rookie
carve-out (`ROOKIE_ENV_MODIFIER_CEILING = 1.10`) remains.  Composite
EV multiplier ranges:

| Position | Floor | Ceiling | Swing |
|---|---|---|---|
| Pitcher (V15.7, was V13) | 0.20 | 1.30 (was 1.55) | 6.5× (was 7.75×) |
| Batter | 0.20 | 1.30 | 6.5× |
| Rookie | 0.20 | 1.10 | 5.5× |

**V15.7 explicitly does NOT change**: V15.6 popularity calibration
(slope=0.22, floor=0.75, ceiling=1.55), V12 multi-pitcher composition
(capped at 1 by V15.3), per-team / per-game caps, anti-correlation
guard, V13.3 position-volume haircut, V13 ML curves, V13 wind-direction
split, V13 catcher framing, V15.4 trait band [0.70, 1.20],
`STACK_BONUS = 1.10`, `MAX_PITCHERS_PER_LINEUP = 1`, T-65 timing,
no-fallbacks rule, no-historical-bleed rule.

**Test impact**: `tests/test_filter_strategy.py::TestV133PositionAndRookie::test_pitcher_does_not_pay_position_multiplier`
updated.  Under V15.7 a pitcher and an OF batter at matched env+trait
have EQUAL EV (no asymmetric-ceiling exploit); the test now verifies
the "no position haircut" invariant against a CATCHER (where the
batter haircut would apply).  Test count unchanged at 258.

**Verification**: 258/258 tests pass; ruff clean; audit_live_isolation
clean.

### V15.6 TV-target popularity recalibration (May 7, T-65 ship)

V15.6 retunes the popularity band against `total_value` (TV) outcomes
after extending `audit_hv_hit_rate.py` to track TV-rate@K alongside
HV-rate@K in a single pass.  No structural changes — same V15
continuous popularity curve, same slot mechanics, same env scoring,
same V12 multi-pitcher composition.  Just three constants:

| Constant | V15.5 | V15.6 |
|---|---|---|
| `POPULARITY_SLOPE` | 0.16 | **0.22** |
| `POPULARITY_MULT_FLOOR` | 0.80 | **0.75** |
| `POPULARITY_MULT_CEILING` | 1.40 | **1.55** |

`POPULARITY_NEUTRAL_SCORE` unchanged at 4.5.

**Why TV is a more informative calibration label than RS or HV-rate.**
The platform's pricing combines RS and card_boost into a single
displayed number (TV).  A contrarian RS=4 player with boost=3 produces
TV=20, beating a star RS=8 player with boost=0 (TV=16).  Across the
42-slate corpus, `corr(pop_score, TV) = -0.31` is ~50% stronger than
`corr(pop_score, RS) = -0.20`.  V15.5's calibration optimised HV-rate
(binary leaderboard hit, indifferent to TV magnitude); V15.6 widens
the band where the TV signal is structurally stronger.

**Sweep methodology** (`scripts/audit_hv_hit_rate.py`, the harness now
reports both metrics).  Slope ∈ [0.00, 0.45], floor ∈ [0.50, 1.00],
ceiling ∈ [1.00, 1.70].  Found a clean Pareto improvement at
slope=0.22 / floor=0.75 / ceiling=1.55:

| Metric | V15.5 | V15.6 | Δ |
|---|---|---|---|
| HV captured @5  | 160 / 717 | 158 / 717 | -2 |
| HV captured @10 | 292 / 717 | 298 / 717 | **+6** |
| HV captured @20 | 483 / 717 | 503 / 717 | **+20** |
| TV captured @5  | 57 / 210 | 58 / 210 | +1 |
| TV captured @10 | 182 / 420 | 189 / 420 | **+7** |
| TV captured @20 | 557 / 840 | 574 / 840 | **+17** |
| Slot-1 in top-5 TV | 17 / 42 | 17 / 42 | preserved |
| Mean slot-1 TV outcome | 17.87 | 17.87 | preserved |

The slot-1 metrics are PRESERVED — V15.6 is purely an improvement on
lineup-wide capture without sacrificing the highest-multiplier slot.
More aggressive configs (floor 0.65, slope 0.30) extracted bigger
HV@20 / TV@20 lift but cost 1 slot-1 hit and feel punitive on stars
with strong env+trait alignment.  V15.6 stays at floor 0.75 — a 25%
max discount — which is the steepest defensible discount that
preserves slot-1 metrics.

**Hard rule reinforced.**  TV is a calibration LABEL (output side).
The runtime never reads `total_value`, `real_score`, or `card_boost`
as inputs.  Same posture as RS and `is_highest_value`.  No boost
predictor, no slot-ordering heuristic that uses boost — boost is dealt
by the platform during the draft, not chosen by the user.  V15.6
exploits the empirical correlation between popularity prediction and
the platform-set boost; it does not predict boost.

**V15.6 explicitly does NOT change**: V12 multi-pitcher 0P-5P
composition chooser (capped at 1 by V15.3), per-team / per-game caps,
anti-correlation guard, V13.3 position-volume haircut, V13 ML curves,
V13 wind-direction split, V13 catcher framing, asymmetric env
ceilings (pitcher 1.55, batter 1.30, rookie 1.10), V15.4 trait band
[0.70, 1.20], `STACK_BONUS = 1.10`, `MAX_PITCHERS_PER_LINEUP = 1`,
T-65 timing, no-fallbacks rule, no-historical-bleed rule.  The audit
isolation script remains clean.

**New audit infrastructure**:
- `scripts/audit_hv_hit_rate.py` — extended to compute and report
  TV-rate@5/@10/@20, slot-1 top-5-TV hit rate, and mean slot-1 TV
  outcome alongside HV-rate metrics.  All env-override env vars
  continue to work for parameter sweeps.
- `scripts/audit_tv_signals.py` — the bucketed signal audit (already
  shipped earlier today): per-quartile mean_RS, mean_TV, HV%, top-5-TV
  rate, plus a popularity × TV cross-tab that empirically validates
  the "popular players need a super-strong RS to overcome low boost"
  thesis (sleeper RS-floor for top-5-TV = 1.80, popular RS-floor = 4.70).
- `scripts/audit_slot1_quality.py` — slot-1 RS / swRS audit, kept
  for trend tracking even though slot-1 metrics are stable in V15.6.

**Verification**: 258/258 tests pass; ruff lint clean; audit_live_isolation
clean.  Calibration is reproducible: re-run the sweep any time the
corpus grows.

### Slot-1 ceiling diagnostic (May 7, post-V15.5)

After V15.5 shipped, a deeper look at the 2026-05-06 slate (15 games,
20 HV winners) surfaced a complementary metric to HV-hit-rate@5: **slot-1
RS quality**.  HV@5 asks "did any of our 5 picks land on the HV
leaderboard?"; slot-1 RS asks "is our highest-multiplier pick the
SLATE's highest-RS HV winner?"  Both are valid proxies for the win
condition; they are not the same thing.

`scripts/audit_slot1_quality.py` (new) replays the env+leverage scoring
stack on the same 42-slate corpus and reports:

| Metric | Value |
|---|---|
| Slot-1 HV-hit rate | 81.0% |
| Avg slot-1 RS | 4.20 |
| Avg slot-weighted RS (zero boost) | 29.90 |
| Avg slate top-HV RS | 7.33 |
| Captured slate top-HV | **1/42 (2.4%)** |

The optimizer's slot-1 pick lands an HV winner 4 out of 5 slates — but
on average that pick is a 4.20-RS contrarian (Matt Waldron / Matt
Wallner archetype), while the slate's top-RS HV winner that day
averaged 7.33 RS (Paul Skenes / Andy Pages archetype).  The gap is
structural: high-RS HV winners are usually stars on Tier-1 markets,
which the V15 popularity model correctly predicts as consensus
picks, applying the 0.80 leverage floor — and the resulting EV cut
drops them to ranks 18-30 even when env + trait are fine.

Slate 5/6 illustrates: Andy Pages (LAD, 7.5 RS, the slot-1 winner of
6+ winning lineups) ranked **#18** with env=0.94, leverage=0.80,
stack=1.10 → EV 82.94. Matt Wallner (MIN, 3.9 RS) ranked **#2** with
env=1.08, leverage=1.40 → EV 151.20. The 1.75× leverage swing
dominated the modest env signal, swapping the high-RS-ceiling pick
for a low-RS-ceiling contrarian.

**Calibration sweep result: HV@5 and slot-1 RS sit on the same Pareto
frontier.**  Across {slope ∈ [0.00, 0.20], floor ∈ [0.80, 1.00], ceil
∈ [1.00, 1.40]} the curve is essentially flat — HV@5 lands in 21–22%,
swRS in 29.7–30.2.  Tighter calibrations (slope=0.09, floor=0.85,
ceil=1.20-1.25) marginally improve slot-1 RS at slight HV@10/@20 cost.
V15.5's calibration is a defensible point on the frontier (best
HV@5).  No calibration retune ships in this pass.

**Why this isn't fixable by tuning popularity alone**: even fully
disabling the discount (floor=1.00) only captures the slate top-HV in
1/42 slates.  The structural ceiling is in env signal sensitivity to
"star on a heavy-fav team in a moderate matchup" — pre-game data
cannot reliably predict a 12-run blowout from a Vegas O/U of 9.5.
This is named here so future calibration work targets either better
env discrimination or a slot-1-aware variant chooser, not yet another
popularity sweep.

The runtime is unchanged.  The audit is a permanent harness in
`scripts/` for tracking calibration tradeoffs across slot-1 RS,
slot-weighted RS, and HV-hit-rate together.

### V15.5 Outcome-Calibrated Popularity Slope (May 7)

V15.5 retunes `POPULARITY_SLOPE` from 0.09 → 0.16 against actual
HV-hit-rate outcomes on the May 2026 historical corpus.  No structural
changes — same V15 continuous popularity curve, same FLOOR (0.80) and
CEILING (1.40), same V15.1 components.  Only the slope inside that band
changes.

**Trigger.** User feedback: "I've never won a draft."  Drafts are won by
landing HV players; the optimizer's top-5 picks were missing the HV
leaderboard too often.  Built `scripts/audit_hv_hit_rate.py` to replay
the live env + leverage stack on every slate in
`historical_players.csv` (37 slates / 1371 rankable players / 617 HV
winners), holding `trait_factor = 1.0` (Statcast kinematics aren't in
the historical CSV) to isolate env + leverage miscalibration.

**Baseline (SLOPE = 0.09, V15.1):**
- HV@5  = 134 / 617 (21.7%, avg 3.62/slate)
- HV@10 = 245 / 617 (39.7%, avg 6.62/slate)
- HV@20 = 406 / 617 (65.8%, avg 10.97/slate)

**Miss decomposition** of 483 HV winners ranked outside top-5 (primary
cause = factor with the largest deviation below 1.0):
- low_env             408 (pitcher 35, batter 373)
- leverage_discount    31 (pitcher 4,  batter 27)
- outranked            30 (pitcher 6,  batter 24)
- position_haircut     14 (pitcher 0,  batter 14)

**Sweep findings.**  ENV_MODIFIER_FLOOR sweep (0.20–0.90) showed HV@5
peaks at the current 0.20 — narrowing the env band trades top-5
capture for top-20 spread, opposite of what we want.  POPULARITY_SLOPE
sweep showed monotonic HV@5 lift up to a plateau around 0.16-0.17:

| SLOPE | HV@5 | HV@10 | HV@20 |
|---|---|---|---|
| 0.00 (no leverage)| 106 (17.2%)| 195 (31.6%) | 377 (61.1%) |
| 0.09 (V15.1)      | 134 (21.7%)| 245 (39.7%) | 406 (65.8%) |
| 0.13              | 133 (21.6%)| 249 (40.4%) | 415 (67.3%) |
| 0.14              | 136 (22.0%)| 248 (40.2%) | 417 (67.6%) |
| 0.16 (V15.5)      | **137 (22.2%)** | **251 (40.7%)** | **419 (67.9%)** |
| 0.17              | 137 (22.2%)| 251 (40.7%) | 420 (68.1%) |
| 0.18              | 136 (22.0%)| 251 (40.7%) | 420 (68.1%) |

Slope=0.16 is the onset of plateau and the round value to commit to.

**Why this differs from `scripts/calibrate_popularity_curve.py`.**  The
existing calibrator returns SLOPE ≈ 0.0952 from the symmetric quantile
fit `(1 − FLOOR) / (p90 − NEUTRAL)` — a curve-shape choice that ensures
exactly the pool's tails saturate at FLOOR and CEILING.  That target is
not the same as "maximise HV-hit-rate@5": empirically the contrarian
end (score 0–2) HV-rates at ~65% vs the consensus end (score 7+) at
~15%, a 4.3× ratio that swamps the FLOOR/CEILING band ratio of 1.75×.
The quantile fit underweights the empirical signal; the
outcome-validated 0.16 captures more of it within the existing
[0.80, 1.40] band by reaching saturation at score deviations of ±2.5
from neutral instead of ±5.5.  Both methods are valid for their
respective targets; HV-hit-rate is the actual contest objective.

**Lift on 37-slate corpus:**
- HV@5  134 → 137 (+3, +2.2%)
- HV@10 245 → 251 (+6, +2.4%)
- HV@20 406 → 419 (+13, +3.2%)

**Surface area:**
- `app/core/constants.py` — `POPULARITY_SLOPE = 0.16` (was 0.09).
  FLOOR / CEILING / NEUTRAL_SCORE unchanged.  Curve-preview comment
  refreshed.
- `scripts/audit_hv_hit_rate.py` (new) — backtest harness.  Reads
  outcome columns from `historical_players.csv`; lives in `/scripts/`
  per the calibration-script carve-out.  Supports `BO_OVERRIDE_*=value`
  env vars for parameter sweeps without code edits, patching both
  `app.core.constants` and `app.core.popularity` (which binds
  POPULARITY_* at import time).
- `scripts/output/hv_miss_decomposition.csv` — output of the harness.

**V15.5 explicitly does NOT change:** popularity score → multiplier
mapping shape (still linear with FLOOR/CEILING clamps), V15.1
continuous components, V15 architectural intent (leverage as
tiebreaker, swing < env swing), V13.3 multi-pitcher composition,
per-team/per-game caps, anti-correlation guard, slot-1 = highest-EV
display rule, T-65 timing, no-fallbacks rule, no-historical-bleed rule.
The audit-isolation script remains clean.

**Verification:** 258/258 tests pass.  `audit_live_isolation.py` clean.
The harness is reproducible: `python scripts/audit_hv_hit_rate.py`
prints the corpus HV@5/@10/@20 and writes the miss-decomposition CSV.

**Known limitation.** The harness holds `trait_factor` constant at 1.0
because the historical CSV doesn't carry Statcast kinematics (exit-velo,
hard-hit%, barrel%, IVB, whiff%) needed to recompute the live trait
score.  Live ranking will differ slightly from the harness because
trait swings are real (band 0.70–1.20).  A follow-up calibration would
need a one-time backfill of Statcast leaderboards onto historical rows
to validate trait-side tuning at the same level of fidelity.

### V15.3 Max-Pitchers Cap + V15.2 Revert (May 6)

V15.3 reinstates the platform-level cap of one pitcher per lineup and
reverts V15.2's batter env ceiling tighten.

**The bug.** V15.2 tightened batter env ceiling 1.30 → 1.20 to address a
"too many rating-30s bats" complaint, but kept pitcher ceiling at 1.55
— widening the pitcher-vs-batter asymmetry from 19% to 29%.  The
mid-slate redeploy on 2026-05-06 produced a lineup of four pitchers and
one batter (Ragans / Cantillo / Soroka / Pérez / Clemens), where the
Real Sports platform actually caps lineups at **1 pitcher**.  Both the
asymmetric over-correction and the unsubmittable pitcher count had to
be fixed before first pitch.

**Two changes:**

1. `ENV_MODIFIER_CEILING` reverted 1.20 → 1.30.  V15.2's tighten was
   tested only against the symmetric case and missed the interaction
   with the asymmetric pitcher ceiling.  The "too many rating-30s bats"
   problem returns under V15.3 but is the lesser evil — and the cap
   below addresses it differently.
2. `MAX_PITCHERS_PER_LINEUP = 1` added to `app/core/constants.py`.  The
   variant chooser (`_enforce_composition`) now iterates only `n_p ∈ [0,
   1]` instead of `[0, 5]`.  Builds 0P+5B and 1P+4B variants only,
   picks the higher slot-weighted total.

**The V12 audit conflict.**  V12.1's documented audit of "35 actual #1
winning lineups" reported 28.6% with 2P+3B, 14.3% with 3P+2B, 11.4%
with 4P+1B — i.e. ~57% of historical winners had multi-pitcher shapes.
That data drove the V12 design ("multi-pitcher 0P-5P composition
chooser").  The 2026-05-06 user assertion ("number of pitchers is maxed
at 1 per draft") directly contradicts that audit.  Possible
explanations:

- Audit data was contaminated (mis-labelled positions on the captured
  leaderboard screenshots, or "pitcher count" counted RP appearances
  separately).
- Platform rules changed since the V12 audit window (March-April 2026).
- The audited "winning lineups" came from a different contest format
  with different rules than the live daily slate.

The runtime fix doesn't depend on resolving which.  V15.3 enforces the
known-current platform rule; if the audit data was right and the
platform actually allows multi-pitcher, we'll see that in lower
slot-weighted RS over the next 5-10 slates and can revisit then.  For
now the structural cap matches what the user can submit.

**Test impact:**
- `tests/test_filter_strategy.py::test_pitcher_only_pool_yields_5p_lineup`
  rewritten to `test_pitcher_only_pool_raises_under_max_pitchers_cap` —
  V12's 5P+0B fallback is no longer reachable; a pitcher-only pool now
  raises `ValueError` from the variant chooser (no legal variant).
- 258/258 tests pass (composition tests already use the constant, not
  hardcoded counts).

### V15.1 Continuous Popularity-Score Components (May 6)

V15 calibrated the score → multiplier curve from outcome data, but the
score *itself* was still built from V14-era binary thresholds: OPS ≥
0.900 → +2 / ERA ≤ 3.00 → +2, fame_count ≥ 1 → +1 / ≥ 3 → +2.  These
were never re-fitted.  V15.1 replaces them with continuous functions
calibrated against actual is_most_popular outcomes.

Triggered by 2026-05-05 user complaint: Bello (BOS, 50% MP rate over
last 2 starts, 7.44 ERA) and Elder (ATL, 0% MP rate, 1.50 ERA) scored
within 17% of each other under V15.  The bucket analysis on the
historical corpus showed why: Bello hit "fame_count ≥ 1" (+1) but
couldn't reach "≥ 3" (+2) because starters only pitch 3× in 14 days;
Elder failed the elite-stats binary at exactly 3.00.  Both edge cases
are boundary-cliff failures of binary thresholds — the kind that recur
indefinitely with one-off fixes.

**Empirical fit (1561 player-slate rows, May 2026):**

| Component | V15 design | V15.1 design | Empirical signal |
|---|---|---|---|
| Elite stats — batter | OPS ≥ 0.900 → +2 (binary) | OPS ramp [0.65, 0.95] → [0, 2.5] pts (continuous) | OPS 0.65 → 7% MP-rate; OPS 0.95 → 64% MP-rate. Smooth gradient. |
| Elite stats — pitcher | ERA ≤ 3.00 → +2 (binary) | ERA ramp [2.50, 4.50] → [2.5, 0] pts (continuous) | ERA <4.0 → ~80% MP-rate; ERA >4.5 → 50–70%. Below-4.0 is "draft-relevant". |
| Fame index | count ≥ 1 → +1, ≥ 3 → +2 (binary) | rate × 3 pts where rate = mp / total appearances (continuous, position-aware window) | rate=0 → 13% MP; rate>0.75 → 86% MP. Massive gradient bucket-thresholds collapsed. |
| Window | 14 days for everyone | 14d batters, 28d pitchers | Pitchers pitch every 5 days; 14d gives ~2-3 starts as denominator (sparse), 28d gives ~5-6 (stable). |
| Market tier | {1:3, 2:2, 3:1, 4:0} | unchanged (data confirms monotone) | Tier 1: 55% MP, Tier 4: 35% MP. Mapping is correct. |
| STAR_PLAYER_FLAGS | +3 if flagged | unchanged (curated list, can't fit) | Flagged: 66% MP, unflagged: 38%. +3 stays. |
| Top-3 batting order | +1 (binary) | unchanged (not in CSV, can't fit) | Live runtime keeps the binary +1. |

**Validation** — AUC on `is_most_popular` against the 1560-row corpus:

| Variant | Overall AUC | Pitcher AUC | Batter AUC |
|---|---|---|---|
| V15 (binary thresholds, current shipped) | 0.7775 | 0.7562 | 0.8018 |
| V15.1 (continuous components) | 0.8213 | 0.7700 | 0.8484 |
| Δ | **+0.0438** | +0.0138 | +0.0466 |

Lift is concentrated on batters (where the OPS ramp + fame-rate signal
both fire) but the pitcher track also gains. The continuous fame rate
is the dominant new lever — separating "popular every start" from
"popular once" instead of collapsing both to identical +1 points.

**Recalibrated curve constants** — V15.1 scores have wider spread (max
~9.0 vs V15's ~7.5) because elite-stats and fame-rate now contribute
proportionally rather than capped at +2/+1 steps.  `POPULARITY_NEUTRAL_SCORE`
re-fit from 3.5 → 4.5 (new pool weighted-mean); `POPULARITY_SLOPE` re-fit
from 0.08 → 0.07 (gentler ramp to match wider score range). Floor and
ceiling unchanged (0.80, 1.25).  Curve preview at calibrated constants:

| Score | Multiplier | HV-rate at this score |
|---|---|---|
| 0 | 1.250 (CEILING / max sleeper boost) | 66% |
| 2 | 1.176 | 65% |
| 4.5 | 1.000 (neutral) | 49% |
| 7 | 0.824 | 19% |
| 9 | 0.800 (FLOOR / max consensus discount) | 16% |

The HV-rate column is the *outcome* signal, not used as input — it's
shown to confirm the multiplier moves in the right direction (low score
= high HV-rate, deserved bonus; high score = low HV-rate, deserved
discount).

**Surface area:**
- `app/core/constants.py` — `LEVERAGE_FAME_INDEX_DAYS_BATTER`,
  `LEVERAGE_FAME_INDEX_DAYS_PITCHER`, `LEVERAGE_FAME_RATE_MAX_PTS`,
  `LEVERAGE_ELITE_BATTER_OPS_FLOOR/CEILING`,
  `LEVERAGE_ELITE_PITCHER_ERA_FLOOR/CEILING`,
  `LEVERAGE_ELITE_STAT_MAX_PTS` replace `LEVERAGE_FAME_INDEX_DAYS`,
  `LEVERAGE_STAR_BATTER_OPS`, `LEVERAGE_STAR_PITCHER_ERA`. Legacy names
  aliased to the new floor/ceiling so `calibrate_popularity_curve.py`
  retains backwards compatibility.
- `app/core/popularity.py` — `_load_fame_rate_index`, `get_fame_rate`,
  `_elite_stat_pts`, `_fame_rate_pts` (private). The public surface
  (`predict_popularity_score`, `predict_rookie_popularity_score`,
  `popularity_score_to_multiplier`) keeps the same signatures —
  callers in `candidate_resolver.py` and `pipeline.py` need no changes.
- `scripts/backfill_player_season_stats_at_slate.py` — extended to also
  populate `era_at_slate` / `whip_at_slate` / `k9_at_slate` columns for
  pitcher rows (was hitter-only).  268/273 pitcher rows backfilled in
  May 2026 (5 unresolved are scraper-OCR name errors, accepted blank).
- `scripts/calibrate_popularity_components.py` (new) — re-fits each
  component's MP-rate curve.  Re-run after any constant change.
- `scripts/calibrate_popularity_curve.py` — updated to consume the V15.1
  score (`_elite_stat_pts` + rate-based fame index).  Output drives
  `POPULARITY_NEUTRAL_SCORE` / `POPULARITY_SLOPE`.
- `tests/test_popularity.py` — `TestContinuousElite` (11 new) and
  `TestContinuousFameRate` pin the ramp shape, monotonicity, no-cliff
  invariant, and position-aware window. 30/30 pass.

**V15.1 explicitly does NOT change:** the popularity score → multiplier
mapping shape (still linear with FLOOR/CEILING clamps), V12 multi-pitcher
0P–5P composition chooser, per-team / per-game caps, anti-correlation
guard, slot-1 = highest-EV-player rule, FADE / hard-exclusion remains
deleted (V11), env asymmetric ceilings (pitcher 1.55, batter 1.30,
rookie 1.10), trait band [0.85, 1.15], V13.3 position-volume haircut,
V13 ML curves, T-65 timing model, no-fallbacks rule, no-historical-bleed
rule. The audit-isolation script remains clean: `app/core/popularity.py`
still in `EXEMPT_FILES` for the prior-slate `is_most_popular` read; no
new outcome-label leakage introduced.

**Verification:** 258/258 tests pass (was 245 pre-V15.1; +13 new tests
in `TestContinuousElite` + `TestContinuousFameRate`).
`scripts/audit_live_isolation.py` clean.  Calibration is reproducible
via `BO_CURRENT_SEASON=2026 python scripts/calibrate_popularity_components.py`.

### V15 Continuous Popularity-Calibrated EV Multiplier (May 6)

V15 replaces V14's discrete five-bucket leverage system (`top_decile` /
`upper_mid` / `mid` / `lower_mid` / `bottom_decile` mapped to fixed
multipliers via `LEVERAGE_FACTORS`) with a **continuous popularity score
in [0, 10] mapping to a continuous EV multiplier** via
`popularity_score_to_multiplier()` in `app/core/popularity.py`. Same
inputs (team market tier, fame flag / elite stats, batting order,
rolling 14-day MP fame index) — smoother gradient.

Triggered by user feedback after the 2026-05-05 slate where the V14
bucket-based picks (Elder, Webb, Vargas, Swanson, Schmitt) missed every
player on the day's top-20 HV leaderboard. The bucket boundaries were
hardcoded slicing — V15 replaces them with a curve calibrated from the
actual outcome distribution in `historical_players.csv`.

**Empirical signal (1561 player-slate rows, May 2026):**

| Pop score | n | HV-rate | MP-rate | alpha (HV/MP) |
|---|---|---|---|---|
| 0.0 | 139 | **71.9%** | 12.2% | 5.88 |
| 1.0 | 180 | 68.9% | 18.3% | 3.76 |
| 2.0 | 276 | 56.5% | 23.6% | 2.40 |
| 3.0 | 241 | 57.3% | 30.3% | 1.89 |
| 4.0 | 196 | 37.8% | 53.6% | 0.71 |
| 5.0 | 203 | 35.5% | 64.0% | 0.55 |
| 6.0 | 155 | 18.1% | 69.0% | 0.26 |
| 7.0 | 111 | 6.3% | 100.0% | 0.06 |

Score-0 players HV at ~12× the field draft rate; score-7 consensus picks
HV at 1/16th the field draft rate. The bucket system collapsed all this
into 5 discrete tiers; the continuous curve preserves the gradient.

**Mechanism** — `_compute_base_ev` in `app/services/filter_strategy.py`:

```
filter_ev = env_factor × volatility_amplifier × trait_factor
          × leverage_factor × stack_bonus × dnp_adj × position_mult × 100

leverage_factor = popularity_score_to_multiplier(predicted_ownership_score)
                = clamp(1.0 + (NEUTRAL - score) * SLOPE, FLOOR, CEILING)
```

Calibrated constants in `app/core/constants.py` (initial V15 ship):
- `POPULARITY_NEUTRAL_SCORE = 3.5` — pool-weighted-mean score; multiplier
  is exactly 1.0 here.
- `POPULARITY_SLOPE = 0.08` — EV change per unit of score.
- `POPULARITY_MULT_FLOOR = 0.80` — heaviest consensus discount.
- `POPULARITY_MULT_CEILING = 1.25` — biggest sleeper premium.

Curve preview:
- Score 0 → 1.25 (max sleeper boost: anonymous Tier-4 small-market batter)
- Score 1 → 1.20
- Score 3.5 → 1.00 (neutral: typical mid-fame mid-market player)
- Score 4 → 0.96 (mild discount: known name on a strong team)
- Score 5 → 0.88
- Score 6+ → 0.80 (floor: heavy consensus — NYY/LAD/BOS star)

V15 is intentionally MORE decisive than V14's [0.85, 1.20] band (1.41×
swing) — V15 hits 1.56× swing — but stays well below env's ~7.75× swing
so leverage remains a tiebreaker, not an override of weak performance.
The user's invariant ("a high-env consensus pick must still rank above a
low-env sleeper") is preserved in `tests/test_filter_strategy.py::TestLeverageFactor::test_leverage_cannot_rescue_weak_candidate`.

**`FilteredCandidate.predicted_ownership_score: float | None`** replaces
the V14 `predicted_ownership_bucket: str | None`. `tests/test_invariants.py::test_predicted_ownership_score_is_allowed` carves it out as a discrete
LABEL (continuous numeric score derived from public observables, NOT a
raw count, NOT an outcome label, NOT card_boost).

**Rookie interaction (preserves V13.3 intent).** Rookie-track players
with no fame, no current-season stats, and small-market teams naturally
score 0–3 → multiplier 1.04–1.25 (boost). Combined with V13.3's
ROOKIE_ENV_MODIFIER_CEILING (1.10) and rookie trait_factor (1.0
neutral), a rookie pitcher's EV ceiling is roughly env=1.10 × trait=1.0
× leverage=1.25 = 1.375 — vs ~1.55 × 1.10 × 0.85 = 1.45 for a
comparable veteran ace under consensus discount. Rookie pitchers stay
competitive in genuinely strong env contexts without dominating EV.
Calls this out in `app/core/popularity.py` docstring and
`tests/test_popularity.py::TestRookiePath::test_rookie_on_tier4_with_no_signals_gets_max_boost`.

**Verification:**
- `scripts/calibrate_popularity_curve.py` (new) — re-runs the
  empirical fit any time the corpus grows. Lives in `/scripts/` because
  it reads outcome columns; the runtime path never reads them.
- 245/245 tests pass post-V15.
- `scripts/audit_live_isolation.py` clean — `app/core/popularity.py`
  remains exempt (reads prior-slate is_most_popular flag only, never
  current slate); no card_boost / drafts / outcome label leakage
  introduced.

**V15 explicitly does NOT change:** V12 multi-pitcher 0P–5P composition
chooser, per-team / per-game caps, anti-correlation guard, slot-1 =
highest-EV-player rule, FADE / hard-exclusion remains deleted (V11),
env asymmetric ceilings (pitcher 1.55, batter 1.30, rookie 1.10), trait
band [0.85, 1.15], V13.3 position-volume haircut, V13 ML curve
inversions, T-65 timing model, no-fallbacks rule, no-historical-bleed
rule.

**Limitations.** Without the original env_score and trait_score values
from each historical slate (computed at T-65 from live MLB / Vegas /
weather APIs and not persisted), the live impact of V15 vs V14 cannot
be deterministically backtested for specific dates. Expected behavior
will surface over the next ~15 slates as more candidates flow through
the new curve.

### V13.3 Position-Volume Haircut + Rookie Env Cap + Stack Bonus Recalibration (May 4)

V13.3 ships four targeted changes from a 40-slate manual audit of slot-1 winners against the live EV math, triggered by user complaints that the optimizer kept defaulting to the same Cubs catcher (Moisés Ballesteros) and a thin-sample SF spot starter (Trevor McDonald, 3 career-recent IP). No structural rewrites — composition, stacking gates, V13 ML curves, trait weights, T-65 timing all unchanged.

**Audit findings (40 rank-1 slot-1 winners across 2026-03-25 → 2026-05-03):**

| Position | Slot-1 wins | % | Avg HV RS | Notes |
|---|---|---|---|---|
| **P** | **25** | **62.5%** | 5.43 | Dominant slot-1 winner |
| OF | 7 | 17.5% | 4.28 | Volume + slot-mult work together |
| DH | 4 | 10.0% | 3.97 | Ohtani 7× alone |
| 1B | 3 | 7.5% | 4.17 | |
| 3B | 1 | 2.5% | 4.18 | |
| **C** | **0** | **0%** | 4.47 | Catchers HV-flag at 5.4% but never anchor |
| 2B | 0 | 0% | 4.51 | |
| SS | 0 | 0% | 4.12 | |

Catchers, 2B, SS: 0/40 slot-1 wins. Catchers get ~30% fewer PAs (rest days, pinch-hits, late pulls — 3.0 PA/game vs 4.2 for OF). 2B/SS have lower OPS distributions. The pre-V13.3 EV math treated all batter positions equally, so an elite-OPS catcher in a stack-eligible game (Ballesteros at CHC -215 / O/U 11.5) topped the EV table over OF/1B/DH and even pitchers.

**Rookie diagnosis (Trevor McDonald, SF, 2026-05-04):** Career 4 G / 18 IP. 2026 zero stats; 2025 zero stats; 2024 single 3-IP start (0.00 ERA / 0.33 WHIP). The combined-IP rookie gate (`ROOKIE_PITCHER_IP_THRESHOLD = 5.0`) flagged him `is_rookie_track=True` after the prior-season fallback returned empty for 2025. Rookie-track scoring sets `trait_factor = 1.0` (neutral) by design. But pre-V13.3 the env_factor was uncapped for rookies — and SF as mild underdog (+116) put him in V13's "underdog peak" ML zone (+1.0), Oracle is pitcher-friendly, O/U was 8.0 (low total bonus) → env_factor saturated near 1.55 (the regular pitcher cap), giving rookie-track McDonald an EV of ~140+ that beat established starters. Underdog teams routinely start unproven pitchers, so V13's underdog ML reward systematically promoted rookies into the lineup — exactly the wrong outcome.

**Four changes:**

**1. `POSITION_VOLUME_MULTIPLIER`** (new) — `app/core/constants.py`. Position-keyed dict applied as a final EV multiplier in `_compute_base_ev`. C: 0.90, 2B: 0.95, SS: 0.95, default 1.0 for OF / 1B / 3B / DH. Pitchers bypass entirely (their own asymmetric env ceiling already handles pitcher-vs-batter EV). Catcher EV is now 10% lower than OF EV in matched env+trait. This is structural (volume math, not fitted), justified by 0% slot-1 wins for catchers/MI in the 40-slate audit.

**2. `ROOKIE_ENV_MODIFIER_CEILING = 1.10`** (new) — `app/core/constants.py`. `_compute_base_ev` selects this ceiling instead of `PITCHER_ENV_MODIFIER_CEILING` (1.55) or `ENV_MODIFIER_CEILING` (1.30) when `candidate.is_rookie_track`. Rookie env_factor now caps just above neutral; an unproven player can't beat a trait-rated player on env alone. The floor stays at `ENV_MODIFIER_FLOOR` (rookies in bad matchups still hit it). `is_rookie_track` is plumbed through `PlayerScoreResult` → `FilteredCandidate` so both `candidate_resolver.py` and `pipeline.py::run_filter_strategy_from_slate` carry it.

**3. `PITCHER_FALLBACK_MIN_PRIOR_IP = 30.0`** (new) — `app/core/constants.py`. Replaces `ROOKIE_PITCHER_IP_THRESHOLD = 5.0` as the rookie-track gate in `data_collection.py`. After the current+prior season fetch, any pitcher with combined IP < 30 is flagged rookie-track. The 5.0 IP threshold caught true debutants but let thin-sample spot starters (6-25 prior IP) slip through with face-value ERA/WHIP/K9. The 30 IP threshold is the "stable-sample" floor — below it, the recent-stats fallback is too noisy to trust.

**4. `STACK_BONUS = 1.10`** (was 1.20) — `app/core/constants.py`. The 40-slate audit shows 0/40 slot-1 winners came from a stacked-team batter — slot-1 winners are pitchers (62.5%) or non-stack OF/DH elites (27.5%). The 20% stack bonus was overweighting stack-eligible-team batters into top-EV without empirical support. 10% still recognises positive correlation upside without dominating the lineup. Existing `tests/test_filter_strategy.py::test_blowout_stack_bonus_applied` continues to pass — it imports `STACK_BONUS` and uses the live value.

**V13.3 explicitly does NOT change:**
- Trait band 0.85-1.15 (initial Tier 1 hypothesis was wrong — widening would help Ballesteros, who already maxes at 1.15)
- `ROOKIE_NEUTRAL_SCORE = 57.5` (unchanged — Ballesteros isn't rookie-track; the rookie cap fix targets the right population)
- V13 ML curves (audit-driven, n=6 small-sample tail not re-litigated)
- Composition (still V12 multi-pitcher 0P-5P chooser)
- Stack-eligibility rules (PATH 1 + PATH 2 unchanged)
- Anti-correlation guard, per-team caps, per-game caps
- T-65 timing model, no-fallbacks rule, no-historical-bleed rule

**Tests added** (tests/test_filter_strategy.py::TestV133PositionAndRookie):
- Catcher EV < OF EV in matched context (precise multiplier check)
- 2B / SS get the smaller 0.95 haircut
- Pitchers bypass position multiplier
- Rookie pitcher env saturates at 1.10
- Non-rookie pitcher env still saturates at 1.55
- Rookie pitcher loses to veteran in matched env (regression on the McDonald case)
- Rookie batter env also capped at 1.10

`ROOKIE_ENV_MODIFIER_CEILING` added to invariant perturbation targets in `tests/test_invariants.py`. 203 tests pass post-V13.3 (was 194; +9 new: 7 V133 + 2 perturbation cells).

**Expected operational effect:**
- Slot-1 leans toward pitchers more often (matching 62.5% historical reality)
- Catchers slot 3-5 instead of slot 1; Ballesteros may still appear (legitimately .978 OPS) but at lower-multiplier slots
- Rookie pitchers in underdog contexts no longer top-EV — established arms reclaim those slots
- Stack-eligible games still produce correlated picks but with 10% bonus, not 20% — non-stack elites compete

### V14 Leverage-Aware EV — Predicted-Ownership Bucket + Contrarian Edge (May 5)

V14 closes the gap the audit doc names in `STRATEGY_AUDIT_2026-05.md`: the V12-V13 pipeline is a calibrated *performance predictor*, but Real Sports daily contests are won by *differentiation from the field*, not by raw mean projection.  The 40-slate corpus shows 92.5% of winning lineups contained at least one Highest Value player who was not on the Most Popular leaderboard, popular HVs and sleeper HVs score essentially identically (mean RS 4.44 vs 4.39), and Most Popular status is highly autocorrelated week-over-week (73.8% of MP appearances have at least one prior MP appearance in the trailing 14 days).  Together these say: the field's draft choices are tracking name recognition rather than performance, the gap is consistent, and a deterministic predictor on public pre-game observables can capture it without ever consuming `drafts` / `card_boost` / outcome labels as live inputs.

**Mechanism** — one new multiplicative term in `_compute_base_ev`:

```
filter_ev = env_factor × volatility_amplifier × trait_factor
          × leverage_factor × stack_bonus × dnp_adj × 100

leverage_factor mapping (predicted_ownership_bucket → multiplier):
    top_decile     → 0.85   (heavily-owned consensus picks: discount)
    upper_mid      → 0.92
    mid            → 1.00   (neutral)
    lower_mid      → 1.08
    bottom_decile  → 1.20   (predicted sleepers: premium)
    None (unknown) → 1.00   (fallback to neutral — only acceptable default
                              because the leverage signal is genuinely
                              additive; missing prediction must not corrupt
                              a valid performance projection)
```

The [0.85, 1.20] band is deliberately narrower than the env factor swing (~7.75x for pitchers, ~6.5x for batters) so leverage acts as a tiebreaker among players with comparable performance projections, never an override of poor env.  A weak performance projection is not elevated to the top of a lineup just because the player is a sleeper — the multiplicative structure ensures `_compute_base_ev` of `(env=0.2, trait=0.8, leverage=1.20)` stays well below `(env=0.85, trait=1.10, leverage=0.85)`.  The composition phase (`_enforce_composition`), per-team caps, anti-correlation guard, slot-1 = highest-EV-player rule, and stack-eligibility paths are all untouched.

**Predicted-ownership bucket** — `app/core/popularity.py::predict_popularity_bucket`.  Deterministic rule-based classifier scoring four families of public pre-game observables to a 0-10 internal score, then mapping to one of five quantile-derived buckets.  No statistical model, no learned weights.  Inputs:

1. **Team market tier** — `TEAM_MARKET_TIER` lookup in `app/core/constants.py`.  Tier 1 (NYY/LAD/BOS/CHC/PHI/NYM) → +3, tier 2 (ATL/STL/SF/HOU/TOR/SD/SEA) → +2, tier 3 (mid-market) → +1, tier 4 (KC/PIT/MIA/ATH/COL/TB/CWS) → 0.  Static; updated once per offseason.
2. **Player fame** — `STAR_PLAYER_FLAGS` (returning All-Stars, MVP/CY top-5 voting, Silver Slugger / Gold Glove winners) → +3.  Or, if not a flagged star, current-season elite stats (OPS ≥ 0.900 / ERA ≤ 3.00) → +2 (only one of these fires per player to avoid double-counting).
3. **Slate context** — top-3 batting order → +1.  Pitchers no batting order signal.
4. **Rolling 14-day fame index** — count of prior Most Popular leaderboard appearances in the trailing 14 days from `historical_players.csv`.  ≥3 appearances → +2, ≥1 → +1.  This is the only input that touches historical data, and only the prior-slate `is_most_popular` flag from dates strictly before today.  Per the audit doc, this is analogous to using prior-season ERA — a backward-looking aggregate of pre-game observables, not leakage of the current slate's outcome.

Bucket cutoffs (internal score → bucket): ≥8.0 = top_decile, ≥6.0 = upper_mid, ≥3.0 = mid, ≥1.5 = lower_mid, else bottom_decile.  Quantile-derived from the 40-slate corpus; re-tune via the standard manual-calibration discipline that governs every other constant in `app/core/constants.py`.

**What V14 explicitly does not do**:
- Does not consume `card_boost` (in-draft, unknowable pre-game).
- Does not consume raw historical `drafts` counts (outcome label).
- Does not consume `real_score`, `total_value`, `is_highest_value`, `is_most_drafted_3x`.
- Does not introduce machine learning.
- Does not change trait weights, env thresholds, stack-eligibility paths, asymmetric env ceilings, multi-pitcher composition search, or any V12/V13 calibration.
- Does not gate behind a feature flag — leverage is live on the next pipeline run.

**Architectural enforcement**:
- `FilteredCandidate.predicted_ownership_bucket: str | None` — the new field is a discrete LABEL, not a count.  Confirmed by `tests/test_invariants.py::TestSignalIsolation::test_predicted_ownership_bucket_is_allowed`.  The dataclass continues to reject `card_boost`, `drafts`, `popularity`, `sharp_score` at construction time.
- `app/core/popularity.py` is in `EXEMPT_FILES` of `scripts/audit_live_isolation.py` because it reads the prior-slate `is_most_popular` flag from `historical_players.csv`.  The exemption is bounded: the module never reads `real_score`, `total_value`, `is_highest_value`, `is_most_drafted_3x`, `drafts`, or `card_boost`, and only counts MP appearances strictly older than the current slate date.
- New `TEAM_MARKET_TIER` validation in `_validate_constants()`: every team in `PARK_HR_FACTORS` must have a tier entry, otherwise the leverage signal silently mutes for that team.
- `tests/test_filter_strategy.py::TestLeverageFactor` pins five contrarian invariants: sleeper outranks consensus at identical performance, leverage band matches `LEVERAGE_FACTORS`, None bucket is neutral, leverage cannot rescue a weak candidate, and `leverage_factor` is recorded on the candidate for response-payload diagnostics.

**Diagnostic exposure** — every `FilterCandidateOut` and `FilterSlotOut` in the `/api/filter-strategy/optimize` response now carries `predicted_ownership_bucket` and `leverage_factor` so the user can inspect why a contrarian player was selected.  Distribution sampled on 2026-05-03 produced ~5% top-decile, ~26% bottom-decile across the candidate pool — a healthy shape for the contrarian signal to do real work.

**Eval impact** — V14 has no offline backtest because the eval harness operates on `historical_slate_results.json` which has no per-slate ownership prediction column.  V14 ships on the architectural argument: forty slates of outcome data show 92.5% of winners contain at least one sleeper HV and the field consistently fades the same player profiles.  Live impact will surface over the next ~15 slates as more bottom_decile-bucket players flow into selected lineups.  Calibration cadence (manual constant review of `LEVERAGE_FACTORS` and the bucket cutoffs in `app/core/popularity.py`) follows the existing discipline; no automated training loop.

### V13.0 Pipeline Audit Pass — ML Curves Inverted, Asymmetric Ceiling Widened, Wind Direction Refined, Framing Bumped (May 2)

V13.0 ships five calibration changes from a fresh 38-slate / 1140-batter / 244-pitcher audit of every active signal against `is_highest_value` and `real_score` outcomes.  No structural changes (composition, stack rules, slot logic, anti-correlation, no-fallbacks all unchanged from V12.2).  This was a pipeline-strengthening pass triggered by an external data-scientist writeup; most of the writeup's claims either (a) validated V12.2, (b) contradicted our 38-slate audit (Vegas O/U for batters re-confirmed flat: 54%/51%/47%/46%/48% across the full quartile range incl. 10.5+ shootouts), or (c) relied on forbidden inputs (`card_boost`, popularity).  Five items were genuine miscalibrations:

**1. Pitcher moneyline curve INVERTED — underdog peak, not mild-fav peak.**

V12.1 documented "mild fav (-120 to -180) HV=37.5%, heavy fav HV=14.5%" on 33 slates.  The 38-slate re-audit refines the curve significantly — underdog pitchers now show the highest HV-rate, not mild favorites:

| ML bucket | n | HV-rate | mean RS |
|---|---|---|---|
| Underdog (≥+100) | 69 | **37.7%** ← peak | 3.09 |
| Mild fav (-180 to -111) | 94 | 29.8% | 3.68 |
| Pickem (-110 to +99) | 27 | 18.5% | 3.21 |
| Clear fav (-250 to -181) | 36 | 16.7% | 3.64 |
| Heavy fav (≤-251) | 6 | **0.0%** ← tank | 2.35 |

Mechanism: underdog pitchers stay in tight games and accumulate K's / pitch deeper while their team scrambles for runs; heavy-favorite teams generate blowouts and pull the starter early before win-bonus and K total stack.  The pre-V13 curve gave underdog +0.2 (tiny bonus, far below mild-fav +1.0); V13 flips this to give underdog the peak +1.0, mild fav +0.8, clear fav +0.2, pickem +0.3, heavy fav -0.2.  Implemented inline in `compute_pitcher_env_score()` ([app/services/filter_strategy.py](app/services/filter_strategy.py)).  The earlier "ML peak at mild fav" claim is preserved in the V12.1 changelog as historical context, but the active behavior is now the V13 curve.

**2. Batter moneyline curve refined — strong underdog premium, fade noise band, deepen heavy-fav penalty.**

V12 captured the underdog premium (+0.3 for ≥+100, -0.2 for ≤-200, +0.2 for "mild fav" -180 to -110) on 35 slates.  V13 38-slate audit shows three calibration misses:

- The +100 flat bonus under-rewarded TRUE underdogs (≥+150).  V13 splits: ≥+150 → +0.5, +100 to +149 → +0.3.
- The -180 to -110 mild-fav band sits in pure noise (Q2 51.6% / Q3 48.4% HV-rate).  V13 zeroes this band.
- The -200 heavy-fav penalty under-penalized vs the audit signal.  V13 splits: ≤-250 → -0.5, -200 to -250 → -0.3.

**3. Wind direction split refined — high-wind IN is volatility-positive, mild-wind IN is suppressive.**

V10.3 added a flat wind-IN penalty.  V13 38-slate audit shows the relationship is non-linear:

| Wind | n | HV-rate |
|---|---|---|
| 10+ OUT | 71 | 64.8% |
| 10+ cross | 154 | 51.9% |
| **10+ IN** | 38 | **52.6%** ← similar to cross |
| 6-9 OUT | 97 | 51.5% |
| 6-9 cross | 340 | 47.9% |
| **6-9 IN** | 56 | **37.5%** ← real penalty zone |
| calm <6 | 309 | 44.7% |

At ≥10 mph, ANY direction lifts HV via volatility (wind disrupts pitch movement, fielder reads, etc.).  But at 6-9 mph IN, there's not enough volatility to compensate for fly-ball suppression.  V13 bumps 10+ IN from +0.1 to +0.3 (matches cross) and adds a -0.2 penalty for 6-9 IN.

**4. Asymmetric env ceiling widened — 1.40 → 1.55 for pitchers.**

V12 documented "pitcher mean RS 34% higher than batter mean RS" justifying the 1.40 vs 1.30 ceiling asymmetry (7.7%).  V13 38-slate audit shows pitcher mean RS is 1.38× batter mean RS (3.42 vs 2.48) — empirical asymmetry is 38%, ceiling asymmetry was 7.7%.  Pitchers were systematically under-rewarded.  V12 backtest also showed our optimizer produced too few multi-pitcher lineups (1P+4B = 37% of output vs 17% of winners; 2P+3B = 23% of output vs 28.6% of winners).  Bumping `PITCHER_ENV_MODIFIER_CEILING` from 1.40 → 1.55 (asymmetry 7.7% → 19%) pushes marginal cases toward the 2P+ shapes that the audit shows actually win more often.

**5. Catcher framing K-rate adjustment magnitude tripled — ±5% → ±12%.**

V10.8 added a ±5% adjustment to the pitcher k_rate trait based on team framing_runs.  V13 audit of own-team framing_runs vs pitcher HV-rate shows Q4 (top framers, ≥+1.06) HV=40.0% vs Q1 (bottom framers, ≤-0.83) HV=21.2% — a +18.8pp swing.  The ±5% trait adjustment translated to ~0.5% EV change after passing through k_rate (35/100 trait weight) → trait_factor (0.85-1.15 narrow band) — structurally too small for an 18.8pp HV signal.  V13 bumps `SCORING_FRAMING_K_RATE_MAX_ADJ` from 0.05 → 0.12.  Stays conservative against the 2026 ABS Challenge System's compression of per-pitch framing effects (2% of pitches are challenged, 98% still human-called).

**V13 explicitly does NOT change:** lineup composition (still V12 multi-pitcher 0P-5P chooser), per-team / per-game caps, anti-correlation guard, stack-eligibility two-path rule, FADE/popularity removal (V11), `compute_total_value()` historical CSV ingest, scoring_engine trait weights (V12.2 zeroing of matchup_quality / lineup_position / ballpark_factor stays — those are env signals), Statcast refresh wiring, T-65 timing model, no-fallbacks rule, no-historical-bleed rule.  Pure calibration improvements from a fresh-eyes audit.

**Eval delta:** None published yet — V13 calibration ships ahead of backtest (the eval harness lives in `/tmp/baseball_eval/`, V12 V13 comparison would be re-run there).  Direction of impact: all five changes correct quantitative miscalibrations against the 38-slate audit, so the live HV-rate over the next ~30 slates should be at-or-above V12.2.  The pitcher ML curve flip is the biggest single change — expect more underdog-pitcher selections (Wheeler in -110 game, Ryan in +120 game) and fewer heavy-favorite-pitcher selections in lineups where the ML-driven pitcher EV used to dominate.

### V12.1 Audit-Driven Rebuild + Multi-Pitcher Variants (April 30)

V12 ships three structural changes from a 35-slate / 994-batter / 222-pitcher quartile audit of every pre-game signal against actual HV outcomes (`is_highest_value` and `real_score` used STRICTLY as outcome labels — never input).

**1. Env scoring rebuilt around audit findings.**

Strong signals kept:
- Opp starter ERA: Q1 (≤3.1) HV=34% → Q4 (≥5.8) HV=57% — biggest single batter signal
- Opp starter WHIP: Q1 (≤1.11) HV=39% → Q4 (≥1.56) HV=55%
- Wind speed ≥10 mph + OUT direction: HV=66% (vs IN: 53%) — survives park control
- Park HR factor (modest for batters, strong for pitchers)
- Underdog ML premium for batters: Q1 (heavy fav -310 to -158) HV=36% vs Q4 (underdog +104 to +250) HV=57%
- Pitcher ML peak at mild fav (-180 to -120): HV=37.5% vs heavy fav (≤-200) HV=14.5%
- Pitcher Vegas O/U inverse: Q1 (≤7.5) HV=31% vs Q4 (≥8.5) HV=18%
- Batting order (volume premium for top of order)

Dead/inverted signals deleted from env:
- Vegas O/U for batters (Q1 50% / Q4 47% — pure noise)
- Opp starter K/9 for batters (Q1 49% / Q4 45% — V10.6 add was wrong)
- Opp bullpen ERA (non-monotonic)
- Opp team K% for pitchers (Q1 25% / Q4 23% — flat)
- Heavy-favorite ML positive bonus (data shows INVERTED — heavy favs underperform)
- Own-team L10 momentum, series leading/trailing (cold/trailing teams produce MORE HV — V10.7 already neutralised, now formally deleted)
- Opp back-to-back rest days (V10.8 add — no audit support)
- Compound park × temp interaction (no audit support)
- Pitcher home-field flat +0.5 (no audit separation)

Constants deleted (149 references swept across the codebase): `BATTER_ENV_VEGAS_*`, `BATTER_ENV_BULLPEN_*`, `BATTER_ENV_OPP_K9_*`, `BATTER_ENV_OPP_BACK_TO_BACK_BONUS`, `BATTER_ENV_GROUP_A_SOFT_CAP_*`, `BATTER_ENV_COMPOUND_*`, `SERIES_LEADING_BONUS`, `SERIES_TRAILING_PENALTY`, `TEAM_HOT_L10_*`, `TEAM_COLD_L10_*`, `PITCHER_ENV_K_PCT_*`, plus the entire constants-table section for env scoring (V12 thresholds are inline in the env score functions for transparency).

**2. Multi-pitcher variants (0P-5P).**

Audit of 35 actual #1 winning lineups:
| Shape | Win-share | Pre-V12? |
|---|---|---|
| 2P+3B | 28.6% ← most common | NO |
| 0P+5B | 25.7% | YES |
| 1P+4B | 17.1% | YES |
| 3P+2B | 14.3% | NO |
| 4P+1B | 11.4% | NO |
| 5P+0B | 2.9% | NO |

Pre-V12 only built {0P+5B, 1P+4B} → structurally incapable of producing 57% of winning shapes. Mean total RS by shape: 0P=17.5, 1P=18.5, 2P=20.3, 3P=22.2, 4P=22.9, 5P=26.6 — **pitcher-heavy lineups score MORE** because individual K/win-bonus games stack into one lineup. V12 builds all 6 variants and picks the highest slot-weighted EV.

**3. Slot 1 = highest-EV player (rearrangement inequality).**

Pre-V12 forced pitcher into slot 1. V12 sorts all 5 by EV descending and assigns to slots 2.0, 1.8, 1.6, 1.4, 1.2. The highest-EV asset belongs in the highest-multiplier slot — putting a 1.4× batter in slot 1 over a 1.3× pitcher is mathematically wrong.

**Calibration changes:**
- `ENV_MODIFIER_FLOOR`: 0.70 → 0.40 → **0.20** (V12.1 sweep). Old floor compressed EV signal (env=0 vs env=1 only 1.86×); empirical RS top-bottom ratio is ~5×.
- `PITCHER_ENV_MODIFIER_CEILING`: 1.20 → **1.40**. V10.6 capped pitchers BELOW batters to fix a saturation bug under old env scoring; V12 scoring is harder to saturate AND empirical pitcher RS is 34% higher than batter RS.

**Backtest results** (35 slates, in-app `run_filter_strategy`):
- Mean slot-weighted RS (with FLAT trait): 27.89
- Slot-1 was an HV player: 68.6%
- BEAT the actual #1 winning lineup (no boost): 28.6% (was ~22% in V11)
- Composition naturally adapted: 0P=29%, 1P=37%, 2P=23%, 3P=6%, 4P=3%, 5P=3%

**Card-boost context** (NOT an input to the model — for interpretation only):
Card boost contributed 55% of winning-lineup totals on average across the 35 slates. Median winning #1 lineup had 2/5 slots at MAX boost (3.0). This means our RS-only picks deliver ~80% of winning-lineup RS BEFORE boost; the user's draft-time boost decisions close the gap. Boost the top-EV player (slot 1) for maximum return.

**V12 explicitly does NOT change**: stack-eligibility logic, per-team / per-game caps, anti-correlation guard, FADE/popularity removal (V11), `compute_total_value()` historical CSV ingest, scoring_engine trait scoring (still uses K/9, ERA, OPS, exit velo, barrel%, etc.), Statcast refresh wiring, T-65 timing model, no-fallbacks rule.

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

1. **RotoWire expected lineups (`app/core/rotowire.py`).** The MLB Stats API only exposes lineup cards 30-60 min before first pitch — typically *after* the T-65 lock. Before V10.4, ~95% of batters at T-65 had `batting_order=None` and got mass-haircut by `DNP_UNKNOWN_PENALTY` (0.85), neutralising the Group B "lineup_position" signal across the entire pool. V10.4 scrapes RotoWire's daily-lineups page (the de-facto source for every open-source MLB DFS optimizer — there is no free first-party API) and pre-fills `SlatePlayer.batting_order` from beat-reporter projections. **Subsequent simplification:** RotoWire is now the single source of truth at T-65 — the previously-attempted MLB boxscore Phase 2 override was dropped because it only ever caught the earliest game on the slate (T-65 fires hours before later games' official cards post). `batting_order_source` records `"rotowire_confirmed"` or `"rotowire_expected"` based on RotoWire's own status flag. **RotoWire is a hard dependency** — fetch failure or zero parseable games raises `RuntimeError`, the T-65 pipeline crashes, and `/optimize` returns HTTP 503. See `app/services/data_collection.py::_enrich_batting_order_from_rotowire`; tests in `tests/test_rotowire.py`.

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

1. **Statcast kinematics wired — runs inside the T-65 pipeline (single trigger).** `scripts/refresh_statcast.py` bulk-loads three Baseball Savant leaderboards via `pybaseball` — exit-velo + barrels (batters), percentile-ranks + arsenal-velocity (pitchers) — and upserts the kinematic columns onto PlayerStats. It's invoked from `app/services/pipeline.py::_refresh_statcast` between `run_fetch_player_stats` and `run_score_slate`. The earlier "pre-warm during monitor sleep" pattern was removed in favour of one trigger / one failure surface — Statcast adds ~30-60s to the T-65 lock window in exchange for eliminating the background-task race condition. When a row is missing on the leaderboard (new call-up, pre-50 BBE), columns stay NULL and the scoring engine routes through its non-Statcast fallback path. Savant fetch failure raises `RuntimeError` under the no-fallbacks rule.
2. **Sacramento (ATH) park factor corrected.** Raised from 0.90 (pre-season guess) to 1.09 (observed 2026 Statcast PF of 1.091 — short RF porch).
3. **Leadoff slot no longer penalised.** `score_lineup_position` gives spots 1-4 equal max points (all top-of-order volume tiers).
4. **card_boost / drafts removed from `FilteredCandidate`.** The optimizer is structurally incapable of consuming them. The router layer joins them from the source `FilterCard` for display only.
5. **`app/services/condition_classifier.py` deleted.** Was unused — only exported entropy/Gini helpers on ownership data.
6. **`MOONSHOT_SAME_TEAM_PENALTY` deleted.** Moonshot naturally diverges from Starting 5 via `sharp_bonus × explosive_bonus`.
7. **`run_filter_strategy_from_slate` call site fixed** (V10.1 patch) — `pipeline.py` no longer passes `card_boost=` to the `FilteredCandidate` constructor; the display join uses a `(name, team) → card_boost` lookup built from `SlatePlayer`.
8. **Rookie Arbitrage baseline** (V10.1 patch) — `score_power_profile` and `score_pitcher_k_rate` now return `UNKNOWN_SCORE_RATIO × max_pts` (neutral baseline) when the player has zero MLB stats AND no Statcast row. Previously both returned 0, which mathematically benched MLB-debut rookies regardless of matchup. Strategy doc §"Rookie Variance Void" — the crowd fades rookies, so we let env/popularity/park decide.

### V10.0 Core Architecture (HISTORICAL — see V12 active behavior section above)

V10.0 introduced the popularity-gate + env/trait modifier-band architecture
that V12 inherits and rebuilt.  Key surviving concepts: env_factor and
trait_factor as multiplicative EV terms; volatility_amplifier; stack_bonus.
**The popularity gate, FADE/TARGET classes, sharp_score, Moonshot, and
all V10.x asymmetric env ceilings have been REMOVED** — see V11.0 and
V12.x changelog entries for the deletions.

### Pre-V12 history (condensed)

The pre-V12 changelog (V2.2 → V11.0) is preserved in the version-specific
section headers above for context.  None of those sections describe
**current** behavior — V12.x supersedes everything.  Highlights:

- **V2-V8** explored graduated penalties, Bayesian dead-capital floors,
  ownership tiers, dynamic pitcher caps, sharp/popularity scrapers, and
  Moonshot dual-lineup architecture.  All retired.
- **V9.0** retired `pop_factor` as an EV multiplier.
- **V10.x** introduced env/trait modifier bands, Statcast kinematics
  + xStats, two-path stacking, EV-driven 0P+5B chooser, three rounds
  of audit-driven calibration (V10.6 / V10.7 / V10.8).
- **V11.0** removed popularity scraping + Moonshot + draft_optimizer
  (the `/api/draft/evaluate` endpoint was leaking `card_boost` into
  pre-draft ranking).
- **V12 / V12.1 / V12.2** rebuilt env scoring from a 35-slate audit,
  added 0P-5P multi-pitcher variants, slot-1 = highest-EV-player,
  rebalanced trait weights to remove env double-counting.  Documented
  in the V12.x section at the top of this file.

### Active behavior summary (V16 Phase 1 — read this, not the changelog)

EV formula (`_compute_base_ev` in `app/services/filter_strategy.py`):
```
filter_ev = env_factor × volatility_amplifier × trait_factor
          × leverage_factor × stack_bonus × dnp_adj × 100

env_factor:        floor 0.20  (V15.7 ceilings — symmetric pitcher/batter)
                   rookie ceiling 1.10  (rookie-track players, V13.3)
                   pitcher ceiling 1.30 (non-rookie pitchers, V15.7)
                   batter ceiling 1.30  (non-rookie batters)
volatility_amplifier: 1 + cv × 0.20 × (env − 0.5) × 2  (batters only)
trait_factor:      floor 0.70, ceiling 1.20  (V15.4 widened band)
leverage_factor:   0.80 (top_decile / consensus) → 1.30 (bottom_decile /
                   sleeper) continuous popularity curve (V16 Phase 1
                   tightened from V15.6's 0.75 / 1.55 against
                   lineup-TV outcomes)
stack_bonus:       1.0 / 1.10 (PATH 1 blowout-fav teams only, V13.3)
dnp_adj:           always 1.0 (strict-mode DNP filter excludes invalid
                   candidates upstream)
position_mult:     REMOVED in V16 Phase 1 (V13.3 catcher 0.90 / 2B-SS
                   0.95 haircut deleted — population prior misfired
                   on individual elite-trait players)
```

V14 leverage_factor pulls from `predicted_ownership_bucket` on the
candidate (set by `app/core/popularity.py::predict_popularity_bucket`
during `resolve_candidates`).  Bucket inputs: team market tier, fame
flag / elite season stats, top-3 batting order, rolling 14-day Most
Popular index from `historical_players.csv` for prior dates only.  See
the V14 changelog above for the full mechanism and audit citation.

Pitcher env ML curve (V13 — inverted from V12's "mild fav peak"):
- Underdog (≥+100):    +1.0  ← peak (HV 37.7%)
- Mild fav (-180..-111): +0.8
- Pickem / clear fav:  +0.2..+0.3 (weak)
- Heavy fav (≤-251):   -0.2 (HV 0%)

Batter env ML curve (V13):
- Strong underdog (≥+150): +0.5
- Underdog (+100..+149):   +0.3
- Mild fav / pickem:       0.0  (V13 deleted noise band)
- Heavy fav (≤-200):       -0.3
- Very heavy fav (≤-250):  -0.5

Composition (`_enforce_composition`):
- Build variants 0P+5B, 1P+4B, 2P+3B, 3P+2B, 4P+1B, 5P+0B.
- For each: top-N pitchers by EV + top-(5-N) batters by EV under
  per-team cap (1 default, 2 stack-eligible) AND anti-correlation
  guard (no opposing batter to any drafted pitcher unless they are
  that pitcher's teammate) AND per-game cap (2).
- Slot-weight each variant: sort all 5 by EV desc, assign multipliers
  2.0, 1.8, 1.6, 1.4, 1.2.  Return the highest-total variant.
- Tiebreak: higher pitcher count wins.

Slot 1 = highest-EV PLAYER regardless of position for variant SELECTION
(rearrangement inequality).  For DISPLAY, the final slot order is
pitcher(s) first then batters in EV-desc order — pitcher count is
unconstrained (0..5 are all legal).

Trait scoring (`scoring_engine.py`) — V12.2 weights remove env
double-counting:
- Pitcher: ace_status 30, k_rate 35, recent_form 20, era_whip 15,
  matchup_quality 0 (was 20 — env handles opp OPS / K%)
- Batter: power_profile 40, recent_form 25, hot_streak 25,
  speed_component 10, matchup_quality 0 / lineup_position 0 /
  ballpark_factor 0 (env handles all three)

Card_boost / drafts / popularity / sharp_score: NEVER on
`FilteredCandidate`, NEVER inputs to env or trait scoring.  Banned at
the dataclass level; enforced by `scripts/audit_live_isolation.py`.

V14 `predicted_ownership_bucket` IS allowed on `FilteredCandidate` — it
is a discrete LABEL (one of top_decile / upper_mid / mid / lower_mid /
bottom_decile) produced from public pre-game observables, NOT a count
and NOT an outcome label.  See the V14 changelog for the audit
carve-out.  `app/core/popularity.py` is `EXEMPT_FILES` in the audit
script because it reads the prior-slate `is_most_popular` flag from
`historical_players.csv`; the read is bounded to dates strictly before
the current slate.

Key functions:
- `run_filter_strategy(candidates, slate_class) → FilterOptimizedLineup`
- `_compute_base_ev(candidate) → float` — single source of EV truth
- `_enforce_composition(candidates, slate_class) → list` — V12 variant chooser
- `_build_variant(n_pitchers, sorted_pitchers, sorted_batters, ...)` — single shape
- `_lineup_total_ev(lineup) → float` — slot-weighted total for variant comparison
- `_smart_slot_assignment(lineup) → slots` — sort-by-EV, assign multipliers desc
- `_team_batter_cap(team, eligible) → int` — 2 for stack-eligible, 1 otherwise

Statcast helpers (`app/core/statcast.py`):
- `get_batter_kinematics(mlb_id, season)` — exit velo, hard-hit%, barrel%
- `get_pitcher_kinematics(mlb_id, season)` — FB velo, IVB, whiff%, chase%

## API Structure (5 routers under `/api/`)

| Router | Prefix | Purpose |
|---|---|---|
| filter-strategy | `/api/filter-strategy` | PRIMARY: single-lineup optimization (V11.0) |
| players | `/api/players` | Player CRUD + search |
| slates | `/api/slates` | Slate management + draft cards + results |
| scoring | `/api/score` | On-demand scoring + rankings (intrinsic scores only — no card_boost) |
| pipeline | `/api/pipeline` | Orchestrated fetch → score → rank |

## Core Rules & Business Logic

1. **Sport-Specific:** This is MLB only. Do NOT add NBA/NFL/etc. logic.
2. **No fallbacks ever.** See "ABSOLUTE RULE" section above. If the pipeline fails, raise an error — never silently serve stale data.
3. **total_value is absolute:** Always `real_score * (2 + card_boost)`. Never null. Computed only via `compute_total_value()` in `app/core/utils.py`.
4. **card_boost is during-draft only.** It must NEVER appear as an input to the scoring engine, EV formula, or any pre-game prediction. V10.0 removed `card_boost` and `drafts` from `FilteredCandidate` entirely — the router reads them from the source `FilterCard` for the response payload. `card_boost` now exists only in: (a) `compute_total_value()` for historical CSV data, (b) DB storage models, (c) request/response schemas. The scoring engine and optimizer are structurally incapable of consuming it.
5. **Enrichment:** Real Sports data does NOT provide Team or Position. The seed script must append standard 3-letter MLB team abbreviations and positions.
6. **Volume:** Ownership volume uses `drafts` column with boolean flags (`is_most_popular`, `is_highest_value`, `is_most_drafted_3x`). Note: `is_most_drafted_3x` is retrospective in the DB — the optimizer recomputes it dynamically each run (top-5 most-drafted with boost ≥ 3.0) so the V2.3 trap penalty fires for live slates.
7. **DRY:** The total_value formula, player lookups, score queries, game log sorting, and linear scaling are centralized in `app/core/utils.py`. League-average defaults and all graduated-scaling thresholds are in `app/core/constants.py`. Never hardcode magic numbers inline.
8. **is_highest_value / is_most_popular flags are retrospective labels.** Never use them as inputs to prediction or optimization — that is a data leak. They reflect post-hoc outcomes only.
9. **No guessing MLB IDs.** If a player name search returns no exact team match, return `None` — never assign the first result as a fallback. Wrong MLB IDs corrupt all downstream stats.

## Strategy (V12.2)

### The scoring formula
```
Player Slot Value = RS × (slot_multiplier + card_boost)
```
Additive (proven from historical data).  Slot multipliers are 2.0, 1.8, 1.6,
1.4, 1.2.  Boost (0.0 - 3.0) is revealed only at draft time and is NEVER
a model input.

### What the model ranks on (env signals — audit-validated)

**Batter env** (compute_batter_env_score):
- Opposing starter ERA — STRONGEST signal (+23pp HV swing across quartiles)
- Opposing starter WHIP (independent of ERA in the corners)
- Wind speed ≥10 mph + OUT direction
- Park HR factor (modest for batters)
- Underdog ML premium (ML +100+ produces MORE HV than heavy favorites)
- Batting order (top-of-order PA volume premium)
- Temperature ≥75°F
- Platoon advantage

Removed dead/inverted signals: Vegas O/U, opp K/9, bullpen ERA, heavy-fav
ML positive bonus, L10 momentum, series leading/trailing, opp rest days,
compound park × temp.

**Pitcher env** (compute_pitcher_env_score):
- Moneyline PEAK at mild fav (-180 to -120 — HV 37.5% vs heavy fav 14.5%)
- Vegas O/U INVERSE (low total = pitcher game)
- Park HR factor (pitcher-friendly)
- K/9 talent (modest bonus)
- ERA tail (small lever, not primary — small sample noise)
- Opp team OPS tail

Removed: opp team K%, heavy-favorite monotonic ML reward, home-field flat
+0.5 (no audit separation).

### What the model ranks on (trait signals)

V12.2 trait weights remove env double-counting.  Active weights:
- Pitcher: ace_status 30 + k_rate 35 + recent_form 20 + era_whip 15
- Batter: power_profile 40 + recent_form 25 + hot_streak 25 + speed 10

Zero-weighted (because env captures them): pitcher matchup_quality, batter
matchup_quality / lineup_position / ballpark_factor.

### Composition (V12 multi-pitcher variants)

Builds variants 0P+5B, 1P+4B, 2P+3B, 3P+2B, 4P+1B, 5P+0B.  For each:
top-N pitchers + top-(5-N) batters by EV under per-team cap (1 default,
2 stack-eligible) AND anti-correlation guard (no opposing batter to
any drafted pitcher unless teammate) AND per-game cap (2).  Slot-weights
each variant by sorting all 5 by EV descending and assigning slot
multipliers 2.0 → 1.2.  Returns the highest-total variant.

### Stack eligibility (`is_stack_eligible_game`)

PATH 1 (favored side only, earns +20% STACK_BONUS): moneyline ≤ -200 AND
O/U ≥ 9.0.  PATH 2 (both sides eligible, no bonus): O/U ≥ 10.5.  Every
other team is capped at 1 batter.

### Slot sequencing
Variant chooser uses rearrangement inequality (highest-EV → 2.0×) for
selection.  Final display reorders: pitcher(s) first by EV, then batters
by EV (so a 1P+4B lineup always shows the pitcher at slot 1).  Slots 2-5 by EV
descending.  Rearrangement inequality: best player in best slot.

## Deployment

- **Dockerfile** + **Procfile** included for Railway
- Environment vars use `BO_` prefix (see `.env.example`)
- SQLite by default — DB is ephemeral by design (Railway containers wipe the file on every restart). `Base.metadata.create_all()` rebuilds the schema in milliseconds at startup. No persistent state lives in the DB; all state is rehydrated from live APIs at T-65.
- Startup does **zero** pipeline work — the T-65 slate monitor is the sole pipeline trigger. Startup logs per-step heartbeats (`STARTUP STEP 1/3 ... STARTUP STEP 3/3 ...`) so any hang is diagnosable from logs alone. The T-65 monitor wraps `startup_done_event.wait()` in a 5-minute watchdog timeout — if startup hangs, the monitor marks the cache failed so `/optimize` returns HTTP 503 instead of spinning on 425 forever.
- If the T-65 pipeline fails, the app returns HTTP 503 from `/api/filter-strategy/optimize` — this is correct behavior

## Mandatory External Dependencies

All of the following must be reachable at T-65 or the pipeline crashes loudly (HTTP 503). No graceful degradation, no fallbacks:

- **MLB Stats API** — schedule, rosters, team season stats, player game logs
- **The Odds API** (`BO_ODDS_API_KEY`) — moneyline + over/under per game
- **RotoWire** (`https://www.rotowire.com/baseball/daily-lineups.php`) — expected lineups (single source of truth for `batting_order` at T-65)
- **Open-Meteo** — weather (temperature + wind)
- **Baseball Savant** (via `pybaseball`) — Statcast leaderboards (kinematics, xStats, team catcher framing)
- **Redis** — frozen-pick cache + monitor state coordination

## Frontend Lock-State

The frontend is structurally gated on the backend's `/api/filter-strategy/status` endpoint, which returns `{ready, phase, lock_time_utc, ...}`. Both SSR (`frontend/src/app/page.tsx`) and the client polling hook (`frontend/src/hooks/useLineupData.ts`) check `/status` first and only fetch `/optimize` when `ready: true`. While not ready, the page renders a clear locked countdown UI (`WaitState`); it never renders an empty/partial picks state. `/status` is dirt cheap (in-memory cache flag read), so the client polls it every 5s without backend cost; `/optimize` is called exactly once per session, when picks are guaranteed frozen.
