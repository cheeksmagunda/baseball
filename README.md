# Ben Oracle

I built this to answer one question: **can we predict the 5 best MLB players to pick on any given day?**

Not a gambling model/betting tool but rather  a daily engine built around a specific question that turns out to be genuinely hard: given everything we know before the games start (matchups, park conditions, weather, Vegas lines, Statcast physics), which 5 players are most likely to have big days (and subsequently end up on the Real Sports daily top performers list)? This is not traditional DFS weighting but rather a model that learns from historical Real Sports app player results and identifies what cindituons produce high value performers without directly trying to guess Real Sports player scoring model. 

The platform this is built for is **Real Sports**, a mobile DFS app with a format that's different from traditional salary-cap DFS. There's no budget to manage. You draft 5 players into 5 fixed multiplier slots (2.0x, 1.8x, 1.6x, 1.4x, 1.2x) and each player card has a boost value (0 to +3.0x) that gets revealed during the draft. Your score is:

```
lineup_score = sum of: real_score x (slot_multiplier + card_boost)
```

Real Score (RS) is Real Sports' proprietary per-game performance metric. RS is a **latent target variable** — the formula is opaque and proprietary. We can observe outcomes (which players land on the HV/Most Popular leaderboards, what their RS was) but we cannot observe the scoring function itself. This means we can't optimize for RS directly. Instead, the system takes a **proxy modeling approach**: use observable pre-game conditions to predict the *relative ordering* of players, calibrate against historical leaderboard outcomes, and treat high EV as a proxy for RS upside. What we can rank on: is this batter facing a starter with a 5.2 ERA and 1.55 WHIP? Is this pitcher's team a mild favorite in a low-total game? Does the Statcast exit velocity profile on this hitter suggest real power or just surface-level stats?

Ben Oracle does exactly that. It pulls live data from six sources at T-65 (65 minutes before first pitch), scores every player on the slate using a rule-based signal model, and outputs one optimized 5-player lineup. The model has been calibrated against 33 real slates of outcome data manually ingested from the Real Sports platform, so every signal in the model has been validated against actual results rather than assumed.

## What makes this a real data science problem

The obvious signals don't work the way you'd expect:

- Heavy favorites (-300+) actually produce **fewer** high-value pitchers than mild favorites (-150 to -120). Blowouts get the starter pulled in the 5th before his strikeout and win-bonus totals stack up.
- Vegas over/under is almost flat at the individual player level (Q1 mean RS: 2.35 vs Q4: 2.62). A high total involves both teams' offenses. The matchup-specific signal you actually want is the opposing starter's ERA and WHIP.
- Hot teams (7-10 wins in last 10 games) produce fewer individual high-value players than cold teams (0-4 wins). Hot teams distribute production across the roster. Cold teams often have one player carrying the offense.
- The winning lineup shape changes every day. Some days 2 pitchers win, some days 0 pitchers win. Pre-V12 the model could only build 0P+5B or 1P+4B lineups and was structurally incapable of producing 57% of actual winning shapes.

Figuring this out took 42 slates of real outcome data and several rounds of quartile-bucketing every pre-game signal against actual HV (Highest Value) results. Signals that were flat or inverted got deleted. Signals with real separation got kept.

## Data sources

Live (T-65) pipeline:

- **MLB Stats API** (`statsapi.mlb.com`) - schedule, rosters, season stats, batting order, game logs. Free, no key required.
- **The Odds API** (`BO_ODDS_API_KEY`) - Vegas moneylines and over/under totals. Required. The pipeline will not run without it.
- **Baseball Savant / Statcast** - exit velocity, barrel rate, hard-hit%, xwOBA, xBA, xSLG, xERA, xwOBA-against, FB velocity, induced vertical break, whiff%, chase%. Pulled via `pybaseball`.
- **RotoWire** - expected batting lineups before the official MLB API posts them. Scrape-based, best-effort, typically available 2-4 hours before first pitch.
- **Open-Meteo** (forecast) - stadium weather per game (temperature, wind speed and direction).
- **Real Sports** - daily slate ingest scraped via `scripts/scrape_realsports_daily.py`. Used only for calibration, never as live pipeline inputs.

Historical-corpus (calibration-only; back-population from public sources):

- **MLB Stats API gumbo feed** (`/api/v1.1/game/{game_pk}/feed/live`) — per-game venue, umpire crew, attendance, duration, day/night, mound visits, ABS challenges, full per-team box-score line, per-pitcher detail (pitch count, IP, hits/runs/ER/BB/SO/HR allowed), bullpen aggregate.
- **MLB Stats API people / standings** — per-player handedness, birth_date, debut_date, height, weight, country, position, jersey number; per-team standings snapshot at slate date (GB, run differential, streak, division rank, home/away record).
- **Baseball Savant leaderboards** — pitcher pitch-arsenal usage % (FF/SI/FC/SL/ST/CU/KC/CH/FS/KN/SV), batter sprint speed + hp-to-first time, OAA + fielding runs prevented, bat-tracking metrics (avg bat speed, hard-swing rate, swing length, squared-up rate).
- **Open-Meteo Archive API** — actual hourly weather at venue lat/lon at first-pitch time (temp, wind, humidity, precipitation, pressure, cloud cover) — distinct from the T-65 forecast captured at slate enrichment.

The corpus stores ~135 external columns spanning all of the above per slate. See "Historical Corpus" below.

> **Note on the Real Sports format:** Card boosts (0 to +3.0x) are additive to the slot multiplier and are revealed during the draft, not before. The optimizer ranks players purely on pre-game signals. Card boost is never an input to the model because it's not knowable before you draft.

---

## Architecture

### T-65 Event-Driven Timing

The core design principle: one pipeline run at exactly T-65 (65 minutes before first pitch), then picks are locked.

```
App Startup (pre-T-65)        T-65 Lock                   Post-Lock
     |                          |                            |
     |- Load cache              |- Fetch MLB data            |- Serve picks
     |- Start monitor           |- Score players             |- Users draft
     +- Sleep (0 API calls)     |- Optimize lineups          |
                                |- Freeze cache              +- Monitor completion
                                +- Lock picks
```

This ensures:
- Fresh data (MLB schedule, game conditions) at the moment of line-locking
- No mid-slate interference from dyno restarts or partial updates
- No fallback to stale data. If the T-65 pipeline fails, `/optimize` returns an error.
- Zero API activity outside the T-65 window

See CLAUDE.md "T-65 Sniper Architecture" for complete timing details.

### Four-Stage Pipeline (Runs Once at T-65)

1. **Collect** (`app/services/data_collection.py`) - Fetch fresh MLB schedule, player stats, game context, Vegas lines, and RotoWire expected lineups for batting-order enrichment
2. **Score** (`app/services/scoring_engine.py`) - Rate each player 0-100 via trait-based profiling (pitchers: 4 active traits, batters: 4 active traits)
3. **Filter** (`app/services/filter_strategy.py`) - Apply V12.2 env scoring rebuilt from a 35-slate quartile audit. Only audit-validated signals are scored: opp ERA, opp WHIP, wind speed, park HR factor, ML mild-fav peak for pitchers, ML underdog premium for batters, batting order, temperature, platoon advantage.
4. **Optimize** (`app/routers/filter_strategy.py`) - Build all six variants (0P+5B through 5P+0B), return the highest slot-weighted EV. Slot 1 (2.0x) goes to the highest-EV player regardless of position. Freeze in cache.

### Philosophy

**The core problem is a latent target variable.** RS is proprietary and opaque — you can observe who ends up on the leaderboard but not the formula that got them there. You cannot optimize directly for RS. The system treats this as a proxy modeling problem: build a rule-based ranking over observable pre-game conditions, validate thresholds against historical leaderboard outcomes (HV/MP membership, real_score), and treat "high EV" as a proxy for "RS upside."

This is not a machine learning model — that's a deliberate choice. A fitted statistical model would optimize toward historical RS outcomes, creating the feedback loop the no-historical-bleed architecture is designed to prevent. Instead, calibration is manual: read historical data, check whether pre-game signals correlate with actual HV outcomes in the right direction, edit constants directly. Interpretable by design, and structurally prevented from leaking outcome data into prediction.

The optimizer ranks players by pre-game conditions (env_factor) and Statcast-driven traits (trait_factor). V12 rebuilt env scoring from scratch using a 35-slate quartile audit against actual HV outcomes. Every dead or inverted signal was deleted. Only audit-validated signals survived. The model is popularity-agnostic: it doesn't favor popular players and doesn't fade unpopular ones. Multi-pitcher variants (0P through 5P) replaced the V11 constraint that could only build 0P or 1P lineups.

Historical data is calibration ground truth only. It is never fed into the live scoring pipeline.

### Stacking

Stacking (multiple batters from the same team) is powerful but correlated. The mini-stack cap is 2 per team and 2 per game. There are two eligibility paths:

- **PATH 1** - moneyline at or below -200 AND vegas total at or above 9.0 (favored side only, earns +20% STACK_BONUS)
- **PATH 2** - vegas total at or above 10.5 (both sides eligible, no bonus since it's already a high-run environment)

Every other team is capped at one batter per lineup. See `is_stack_eligible_game()` in `app/core/constants.py`.

### Pre-Card Lineup Harvesting (RotoWire integration)

The MLB Stats API only exposes official lineup cards 30-60 minutes before first pitch, which is typically after the T-65 lock. Without external data, around 95% of batters at T-65 would have no batting order and get penalized by the DNP_UNKNOWN_PENALTY (0.93), killing the lineup-position signal across the entire pool.

V10.4 added a RotoWire scrape that pre-fills `SlatePlayer.batting_order` from beat-reporter projections up to 4 hours before first pitch. The official MLB API boxscore overrides RotoWire as ground truth when posted. The `batting_order_source` column records provenance (`"rotowire_confirmed"`, `"rotowire_expected"`, or `"official"`). RotoWire failures log loudly but don't crash the pipeline since missing batting orders degrade gracefully through the DNP penalty.

---

## Scoring Engine

### Pitcher Traits (V12.2 weights, sum to 100)

| Trait | Weight | What It Measures |
|---|---|---|
| ace_status | 30 | ERA-based rotation rank proxy |
| k_rate | 35 | Statcast (FB velo / IVB / extension / whiff% / chase%, 70%) + K/9 (30%). Includes +/-5% catcher-framing scaling. |
| recent_form | 20 | Last 3 starts quality |
| era_whip | 15 | Live ERA (45%) + WHIP (30%) + xERA (25%) |
| matchup_quality | 0 | Zeroed in V12.2. Env factor handles opp OPS and K%, removing the double-count. |

### Batter Traits (V12.2 weights, sum to 100)

| Trait | Weight | What It Measures |
|---|---|---|
| power_profile | 40 | Avg EV (7) / hard-hit% (7) / barrel% (6) / xwOBA (4) / max EV (1) |
| recent_form | 25 | Last 7 games production |
| hot_streak | 25 | Multi-hit games in last 3 |
| speed_component | 10 | Stolen base pace |
| matchup_quality | 0 | Zeroed in V12.2. Env factor handles opp ERA/WHIP. |
| lineup_position | 0 | Zeroed in V12.2. Env factor handles batting order. |
| ballpark_factor | 0 | Zeroed in V12.2. Env factor handles park HR, wind, and temperature. |

The scoring engine outputs a 0-100 ranking signal, not an RS prediction.

**Statcast refresh:** Exit velocity, barrel%, hard-hit%, FB velocity, IVB, extension, whiff%, chase%, xwOBA, xBA, xSLG, xERA, and xwOBA-against are populated by `scripts/refresh_statcast.py`, invoked inline by the T-65 pipeline between stat fetch and scoring. If Statcast fails, the pipeline crashes and `/optimize` returns 503 rather than serving a lineup built on NULL kinematics. True MLB debutants (no current-season + no prior-season stats) are routed to the V13.2 rookie scoring track — neutral trait_factor, env-only EV — so they do not require Statcast or traditional stats.

### Fail-loud data fetches

Every live-data fetch used by the T-65 pipeline either succeeds or fails loud. There are no silent fallbacks. The DB schema is rebuilt from SQLAlchemy models on every startup; there are no Alembic migrations.

| Path | On failure |
|---|---|
| Schema build (`init_db()` at startup) | App lifespan crashes |
| Statcast kinematics refresh (inline at T-65) | Non-zero exit calls `lineup_cache.mark_failed()`, `/optimize` returns 503 |
| Vegas lines (Odds API) | RuntimeError if key missing, quota exhausted, or any game without lines |
| Weather (Open-Meteo) | RuntimeError on any non-2xx response, including 429 |
| Series context + rest days | Raises if any team's schedule lookback fails |
| Team batting/pitching stats | Raises if any field is NULL post-fetch |
| RotoWire expected lineups | RuntimeError on network failure or zero parseable games |
| Probable-starter ERA/WHIP/K9 | Raises unless the starter is rookie-track (V13.2) |

---

## Optimizer (V12.2)

Single lineup, multi-pitcher variants (0P through 5P), audit-validated env signals. Slot 1 (2.0x) goes to the highest-EV player regardless of position.

**EV formula:**

```
filter_ev = env_factor x volatility_amplifier x trait_factor x stack_bonus x dnp_adj x 100
```

| Signal | Range | Role |
|---|---|---|
| env_factor (pitchers) | 0.20 to 1.40 | ML mild-fav peak, Vegas O/U inverse, park HR, K/9, ERA tail, opp OPS |
| env_factor (batters) | 0.20 to 1.30 | Opp ERA (strongest signal), opp WHIP, wind speed, park, ML underdog premium, batting order, temp, platoon |
| volatility_amplifier | 0.8 to 1.2 (batters only) | Env-conditional: high-CV hitters get +20% in good environments, -20% in bad ones |
| trait_factor | 0.85 to 1.15 | Intrinsic player talent from Statcast, independent of env |
| stack_bonus | 1.0 or 1.20 | PATH 1 blowout-favorite bonus only |
| dnp_adj | 0.70 / 0.93 / 1.00 | Confirmed-bad / unknown / known batting order |

**Composition:** For each pitcher count from 0 to 5, the optimizer picks the top-N pitchers and top-(5-N) batters by EV under the per-team cap (1 default, 2 stack-eligible), anti-correlation guard (no opposing batter to any drafted pitcher unless they're that pitcher's teammate), and per-game cap (2). Each variant is slot-weighted by sorting all 5 players by EV descending and assigning multipliers 2.0 through 1.2. The variant with the highest slot-weighted total wins.

**Anti-correlation guard:** A batter is blocked from any game where one of the drafted pitchers is pitching, unless the batter is on that pitcher's team.

---

## Strategy Insights (what 33 slates of data actually showed)

- **Two-path stacking captures the highest-leverage games.** PATH 2 shootouts (O/U at or above 10.5) yield 2.31 HV players per game vs 1.22 baseline (+89%). PATH 1 blowouts yield 1.50 HV per game (+23%). The mini-stack cap of 2 captures the correlation edge without over-committing to one game.
- **Mild-favorite pitchers outperform heavy-favorite pitchers.** Bucketing pitcher team ML by quartile: heavy fav (-310 to -168) mean_rs 3.12 / HV-rate 12.7%. Mild fav (-164 to -120) mean_rs 4.20 / HV-rate 38.2%. Heavy favorites generate blowouts where the starter gets pulled in the 5th or 6th inning before strikeout and win-bonus totals stack up. V10.7 tightened PITCHER_ENV_ML_CEILING from -220 to -150 so the curve saturates at the mild-fav peak rather than rewarding blowout scenarios.
- **Team momentum signals are inverted at the individual player level.** Cold teams (0-4 wins in L10) produced mean_rs 2.86. Hot teams (7-10 wins) produced 2.40. Same inversion on series lead vs series trailing. Hot and leading teams distribute production across the roster. Cold and trailing teams often have one player carrying the offense. V10.7 neutralised both signals rather than reversing them, since the 33-slate sample isn't large enough to confidently flip a sign.
- **Vegas O/U is barely a player-level signal.** Bucketing 983 batter-slates by O/U: Q1 (6.5-7.5) mean_rs 2.35 vs Q4 (9.0+) mean_rs 2.62. That's a 1.04x swing, basically noise. O/U bakes in both teams' offenses. The actual matchup-specific signal is the opposing starter's ERA and WHIP.
- **Pitchers were saturating the top-10 before V10.6.** An offline evaluation on the 33-slate corpus showed 54% of the model's top-10 were pitchers (target was around 40%). Tightening the pitcher env ceiling to 1.20x and adjusting the DNP_UNKNOWN_PENALTY brought pitcher share to 39.7%.
- **V10.8 signal expansion:** Added Statcast xStats (xwOBA and xERA), opposing arsenal effectiveness via xwOBA-against, conservative +/-5% catcher framing scaling (throttled for the 2026 ABS Challenge System era), and opponent rest days as a batter bonus when the opposing team played the night before.
- **Eval progression:** V10.5 baseline HV@20 = 8.24/20. V10.6: 8.73/20. V10.7: 9.15/20. V10.8: 9.18/20 (above the 8-9 target range).

### Research citations consulted during signal development

- [MLB Glossary on xwOBA](https://www.mlb.com/glossary/statcast/expected-woba)
- [Baseball Savant Expected Statistics Leaderboard](https://baseballsavant.mlb.com/leaderboard/expected_statistics)
- [pybaseball (Statcast wrapper)](https://github.com/jldbc/pybaseball)
- [MLB.com 2026 ABS Challenge System announcement](https://www.mlb.com/news/abs-challenge-system-mlb-2026)
- [TruMedia Catcher Framing model (3.9% K-rate per framing run)](https://baseball.help.trumedianetworks.com/baseball/catcher-framing-model)
- [FanGraphs: Effect of Rest Days on Starting Pitcher Performance](https://community.fangraphs.com/the-effect-of-rest-days-on-starting-pitcher-performance/)
- [FantasyLabs: Opponent Rest Days as DFS edge](https://www.fantasylabs.com/articles/how-does-a-well-rested-opponent-affect-hitters-and-pitchers/)

---

## Historical Corpus

`data/historical.db` is the canonical SQLite store backing every calibration sweep.  Five logical tables:

| Table | Rows | Role |
|---|---|---|
| `slate` | 43 | One per slate envelope (date, game count, source, num_brawlers) |
| `slate_game` | 551 | Per-game env signals + post-game outcomes — **160 columns** spanning Vegas, weather (forecast + actual), starter ERA/WHIP/K9/xERA, team OPS/K%/bullpen ERA/framing, standings snapshot (GB, run differential, streak, rank, home/away record), venue static (capacity, surface, roof, elevation, lat/lon, timezone, field dimensions), umpire crew, catcher mlb_id, attendance, day/night, mound visits, ABS challenges, full per-team box-score line, per-pitcher detail (pitch count, IP, hits/R/ER/BB/SO/HR allowed), bullpen aggregate. |
| `player_slate` | 1644 | Per-(slate_date, mlb_id) identity + at-slate inputs — **60 columns** spanning OPS / ERA / WHIP / K9 at slate, platoon splits, batting-order slot, Statcast kinematics (xwOBA / xBA / xSLG / avg EV / hard-hit% / barrel% / max EV / FB velo / IVB / extension / whiff% / chase%), pitcher arsenal usage % per pitch type (FF/SI/FC/SL/ST/CU/KC/CH/FS/KN/SV) + dominant pitch, sprint speed + hp-to-first, OAA + fielding-runs-prevented, bat tracking (avg bat speed, hard-swing rate, swing length, squared-up rate, blast rate, swords count), plus stable per-player externals (handedness, birth_date, mlb_debut_date, height, weight, country, position, jersey). |
| `player_game_log` | 12290 | Prior 10-game window per (slate_date, mlb_id) for `recent_form` / `hot_streak` calibration. |
| `label_event` | 21945 | Outcome labels (typed, sourced, dated): `real_score`, `total_value`, `card_boost`, `drafts`, draft-shape rollups, three leaderboard membership flags (`highest_value` / `most_popular` / `most_drafted_3x`), `winning_lineup_slot` (per ranked lineup), `box_score` (HV-only post-game JSON), `most_common_slot`, `injury_status`. |

**Architecture rule preserved:** the live runtime never reads outcome labels or any column with the word "outcome" in its docstring.  The only `app/` reader of the corpus is `app/core/popularity.py`, which queries `label_event` for the rolling 14-day `most_popular` index — a backward-looking aggregate of pre-game observables, not leakage of the current slate's outcome.  See `scripts/audit_live_isolation.py` for the carve-out.

**Source-of-truth tooling:**

```bash
# Rebuild data/historical.db from the on-disk CSVs/JSON
python scripts/build_historical_db.py --rebuild

# Refresh the 5 derived /data/ exports from data/historical.db
python scripts/export_historical_csvs.py

# Run all 10 external backfills (idempotent; cache hits make re-runs fast)
for s in scripts/backfill_game_externals.py \
         scripts/backfill_pitcher_boxscore.py \
         scripts/backfill_player_externals.py \
         scripts/backfill_pitcher_arsenal.py \
         scripts/backfill_team_boxscore.py \
         scripts/backfill_weather_actuals.py \
         scripts/backfill_sprint_oaa.py \
         scripts/backfill_standings.py \
         scripts/backfill_game_meta.py \
         scripts/backfill_bat_tracking.py; do python "$s"; done

# Run the comprehensive corpus audit (writes scripts/output/historical_corpus_audit.txt)
python scripts/audit_historical_corpus.py
```

Per-source disk caches under `scripts/output/.*_cache/` make re-runs cheap — only newly-added slates hit the network.

---

## API Endpoints

All endpoints are under `/api/`.

### Filter Strategy (Primary)
| Method | Path | Description |
|---|---|---|
| GET | `/api/filter-strategy/status` | T-65 countdown, cache state, first-pitch time |
| GET | `/api/filter-strategy/optimize` | Serve the frozen single lineup |
| POST | `/api/filter-strategy/classify-slate` | Classify a slate without running the optimizer |
| GET | `/api/filter-strategy/diagnostics` | Pipeline health dashboard |

### Slates
| Method | Path | Description |
|---|---|---|
| GET | `/api/slates` | List all slates |
| GET | `/api/slates/{date}` | Get slate by date |
| GET | `/api/slates/{date}/players` | Get slate players |
| POST | `/api/slates/{date}/players` | Add draft cards to slate |
| PUT | `/api/slates/{date}/results` | Upload actual RS results |

### Players
| Method | Path | Description |
|---|---|---|
| GET | `/api/players` | List players (with filters) |
| GET | `/api/players/{id}` | Get player detail |

### Scoring
| Method | Path | Description |
|---|---|---|
| POST | `/api/score/player` | Score a single player |
| POST | `/api/score/slate/{date}` | Score all players for a slate |
| GET | `/api/score/{date}/rankings` | Get cached rankings |

### Pipeline (Post-slate testing only, gated during live slates)
| Method | Path | Description |
|---|---|---|
| POST | `/api/pipeline/fetch/{date}` | Fetch schedule and stats |
| POST | `/api/pipeline/score/{date}` | Score a slate |
| POST | `/api/pipeline/run/{date}` | Full pipeline (fetch through score) |
| POST | `/api/pipeline/filter-strategy/{date}` | Full pipeline with filter strategy |

---

## Tech Stack

- **FastAPI** - REST API framework
- **SQLAlchemy** - ORM with SQLite (swappable to Postgres via `BO_DATABASE_URL`); schema rebuilt from models at startup
- **Redis** - Required cache layer. Startup fails without it.
- **Pydantic** - Request/response validation
- **httpx** - Async HTTP client (module-level keep-alive) for MLB Stats API, Odds API, Open-Meteo, RotoWire
- **tenacity** - Full-jitter exponential retries on every external fetch
- **NumPy** - Calibration metrics
- **pybaseball** - Statcast data wrapper for Baseball Savant
- **Railway** - Deployment target (single-replica only, since the T-65 monitor is an in-process singleton)

---

## Project Structure

```
app/
+-- main.py                 # FastAPI app + CORS + lifespan
+-- config.py               # pydantic-settings (BO_ prefix)
+-- database.py             # SQLAlchemy engine + session
+-- core/
|   +-- constants.py        # Slot multipliers, RS ranges, park factors, all thresholds
|   +-- weights.py          # Configurable scoring weights
|   +-- utils.py            # Shared formulas (compute_total_value, scale_score, etc.)
|   +-- mlb_api.py          # MLB Stats API client
|   +-- odds_api.py         # The Odds API client (Vegas moneyline + O/U)
|   +-- open_meteo.py       # Weather API client (temperature, wind)
|   +-- statcast.py         # Baseball Savant kinematics
|   +-- rotowire.py         # RotoWire daily-lineups parser
|   +-- historical_db.py    # Historical-corpus SQLite schema + helpers
|   +-- popularity.py       # Predicted-popularity / leverage signal (V14+)
+-- models/
|   +-- player.py           # Player, PlayerStats, PlayerGameLog, TeamSeasonStats
|   +-- slate.py            # Slate, SlateGame, SlatePlayer
|   +-- scoring.py          # PlayerScore, ScoreBreakdown
+-- schemas/                # Pydantic request/response models
+-- routers/                # API route handlers
+-- services/
    +-- scoring_engine.py   # Trait-based scorer (0-100)
    +-- filter_strategy.py  # EV pipeline + single-lineup optimizer
    +-- candidate_resolver.py  # Builds FilteredCandidate pool from DB
    +-- lineup_cache.py     # Frozen-cache invariants (Redis + SQLite persistence)
    +-- slate_monitor.py    # T-65 event loop
    +-- data_collection.py  # MLB API + RotoWire data fetching
    +-- pipeline.py         # Fetch, Score, Rank orchestrator
data/
+-- historical.db                    # Canonical SQLite store (5 tables) — see "Historical Corpus" below
+-- historical_players.csv           # Derived export: master player ledger (1644 rows)
+-- historical_winning_drafts.csv    # Derived export: top-ranked lineups, 5 slots per lineup
+-- historical_slate_results.json    # Derived export: per-date slate envelope (160-col game shape)
+-- historical_player_game_logs.csv  # Derived export: prior 10-game window per player
+-- hv_player_game_stats.csv         # Derived export: box scores for Highest Value players
```

Current coverage: 2026-03-25 through 2026-05-07 (43 slates).  The 5 on-disk files are byte-stable derived exports of `data/historical.db` refreshed by every writer.

---

## Getting Started

```bash
# Install dependencies
pip install -e ".[dev]"

# Copy and fill in environment variables
cp .env.example .env

# Run the server (DB schema is rebuilt from SQLAlchemy models on every startup)
uvicorn app.main:app --reload

# Run tests
pytest tests/
```

### Claude Code cloud

`scripts/cloud-setup.sh` is a reference copy of the bootstrap script that runs when the Claude Code cloud container is provisioned. The canonical runtime lives in the Claude Code cloud environment "Setup script" field. Paste the contents of `scripts/cloud-setup.sh` there to restore or rebuild the environment. The repo copy does not auto-execute.

The script requires `GITHUB_PAT` in cloud secrets (for `gh` CLI and `git push` auth) plus the `BO_*` env vars listed in `.env.example`.

---

## Ingesting a New Slate

The default daily ingest is automated via a Playwright scraper (`scripts/scrape_realsports_daily.py`) that captures the day's leaderboards (HV/MP/3X), top-20 winning lineups, and game results from Real Sports' internal JSON endpoints, writes them to `data/historical.db`, and refreshes the 5 derived /data/ exports.

```bash
# scrape yesterday's slate (writes SQLite + refreshes CSVs/JSON)
.venv-scraper/bin/python scripts/scrape_realsports_daily.py

# enrich Vegas / weather forecast / starter ERA-WHIP-K9 / opp rest days
.venv/bin/python scripts/backfill_slate_env_conditions.py

# pull box-score detail + season-stats-at-slate snapshot
.venv/bin/python scripts/backfill_slate_results_and_hv_stats.py
.venv/bin/python scripts/backfill_player_season_stats_at_slate.py

# back-populate the ~135 external-data columns (one pass; cache hits where possible)
.venv/bin/python scripts/backfill_game_externals.py       # venue / umpire / catcher / attendance / duration
.venv/bin/python scripts/backfill_pitcher_boxscore.py     # per-starter pitch count + outs + hits/R/ER/BB/SO/HR
.venv/bin/python scripts/backfill_team_boxscore.py        # per-team hits/HR/SO/BB/LOB/SB/errors + innings_played
.venv/bin/python scripts/backfill_player_externals.py     # handedness / birth / debut / physicals
.venv/bin/python scripts/backfill_pitcher_arsenal.py      # per-pitcher pitch arsenal % via Savant
.venv/bin/python scripts/backfill_sprint_oaa.py           # batter sprint speed + OAA via Savant
.venv/bin/python scripts/backfill_bat_tracking.py         # bat speed / hard-swing rate via Savant
.venv/bin/python scripts/backfill_weather_actuals.py      # actual hourly weather via Open-Meteo Archive
.venv/bin/python scripts/backfill_standings.py            # GB / run differential / streak / rank
.venv/bin/python scripts/backfill_game_meta.py            # mound visits + ABS challenges

# verify lockstep + integrity
.venv/bin/python scripts/validate_ingest.py --date YYYY-MM-DD
.venv/bin/python scripts/audit_historical_corpus.py
```

The scraper reads its auth from `scraper/storage_state.json` (gitignored). If the token expires, refresh it once interactively:

```bash
BO_REALSPORTS_PASSWORD=… .venv-scraper/bin/python scripts/scrape_realsports_daily.py --refresh-auth
```

Manual fallback (screenshot capture + row-by-row append) is documented in CLAUDE.md §"Improved Ingest Process (V9.1)" for cases where the platform layout changes and breaks scraper selectors.

The historical-corpus SQLite (`data/historical.db`) is the canonical store. The 5 on-disk CSV/JSON files are byte-stable derived exports refreshed by every writer; calibration scripts read SQLite directly via `app.core.historical_db.connect_readonly()`.

---

## Deployment (Railway)

The app includes a `Dockerfile` and `Procfile` for Railway deployment. Set `BO_DATABASE_URL` and `PORT` as environment variables. The database schema is rebuilt from SQLAlchemy models (`Base.metadata.create_all()`) on every startup — there are no Alembic migrations because the DB stores only current-cycle live state and is ephemeral by design.

---

## License

Private project.
