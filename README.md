# Ben Oracle

I built this to answer one question: **can we predict the 5 best MLB players to pick on any given day?**

Not a gambling model/betting tool but rather  a daily engine built around a specific question that turns out to be genuinely hard: given everything we know before the games start (matchups, park conditions, weather, Vegas lines, Statcast physics), which 5 players are most likely to have big days (and subsequently end up on the Real Sports daily top performers list)? This is not traditional DFS weighting but rather a model that learns from historical Real Sports app player results and identifies what cindituons produce high value performers without directly trying to guess Real Sports player scoring model. 

The platform this is built for is **Real Sports**, a mobile DFS app with a format that's different from traditional salary-cap DFS. There's no budget to manage. You draft 5 players into 5 fixed multiplier slots (2.0x, 1.8x, 1.6x, 1.4x, 1.2x) and each player card has a boost value (0 to +3.0x) that gets revealed during the draft. Your score is:

```
lineup_score = sum of: real_score x (slot_multiplier + card_boost)
```

Real Score (RS) is Real Sports' proprietary per-game performance metric. We can't predict it directly because it's platform-specific and opaque. What we can do is rank players by the pre-game conditions that actually correlate with high RS: is this batter facing a starter with a 5.2 ERA and 1.55 WHIP? Is this pitcher's team a mild favorite in a low-total game? Does the Statcast exit velocity profile on this hitter suggest real power or just surface-level stats?

Ben Oracle does exactly that. It pulls live data from six sources at T-65 (65 minutes before first pitch), scores every player on the slate using a rule-based signal model, and outputs one optimized 5-player lineup. The model has been calibrated against 33 real slates of outcome data manually ingested from the Real Sports platform, so every signal in the model has been validated against actual results rather than assumed.

## What makes this a real data science problem

The obvious signals don't work the way you'd expect:

- Heavy favorites (-300+) actually produce **fewer** high-value pitchers than mild favorites (-150 to -120). Blowouts get the starter pulled in the 5th before his strikeout and win-bonus totals stack up.
- Vegas over/under is almost flat at the individual player level (Q1 mean RS: 2.35 vs Q4: 2.62). A high total involves both teams' offenses. The matchup-specific signal you actually want is the opposing starter's ERA and WHIP.
- Hot teams (7-10 wins in last 10 games) produce fewer individual high-value players than cold teams (0-4 wins). Hot teams distribute production across the roster. Cold teams often have one player carrying the offense.
- The winning lineup shape changes every day. Some days 2 pitchers win, some days 0 pitchers win. Pre-V12 the model could only build 0P+5B or 1P+4B lineups and was structurally incapable of producing 57% of actual winning shapes.

Figuring this out took 33 slates of real outcome data and several rounds of quartile-bucketing every pre-game signal against actual HV (Highest Value) results. Signals that were flat or inverted got deleted. Signals with real separation got kept.

## Data sources

- **MLB Stats API** (`statsapi.mlb.com`) - schedule, rosters, season stats, batting order, game logs. Free, no key required.
- **The Odds API** (`BO_ODDS_API_KEY`) - Vegas moneylines and over/under totals. Required. The pipeline will not run without it.
- **Baseball Savant / Statcast** - exit velocity, barrel rate, hard-hit%, xwOBA, xBA, xSLG, xERA, xwOBA-against, FB velocity, induced vertical break, whiff%, chase%. Pulled via `pybaseball`.
- **RotoWire** - expected batting lineups before the official MLB API posts them. Scrape-based, best-effort, typically available 2-4 hours before first pitch.
- **Open-Meteo** - stadium weather per game (temperature, wind speed and direction).
- **Real Sports** - 33 slates of historical outcome data (manually ingested from the platform UI). Used only for calibration, never as live pipeline inputs.

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

This is not a machine learning model. It's a rule-based scoring engine backed by live API data. The goal is to win drafts, not predict Real Score directly. RS is opaque, so the optimizer ranks players by pre-game conditions (env_factor) and Statcast-driven traits (trait_factor).

V12 rebuilt the env scoring from scratch using a 35-slate audit against actual HV outcomes. Every dead or inverted signal was deleted. Only audit-validated signals survived. The model is popularity-agnostic: it doesn't favor popular players and doesn't fade unpopular ones. Multi-pitcher variants (0P through 5P) replaced the V11 constraint that could only build 0P or 1P lineups.

Historical stats are reference data only. They are never fed into the live scoring pipeline.

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

**Statcast refresh:** Exit velocity, barrel%, hard-hit%, FB velocity, IVB, extension, whiff%, chase%, xwOBA, xBA, xSLG, xERA, and xwOBA-against are populated by `scripts/refresh_statcast.py`. The slate monitor fires this script in the background during Phase 2 (before the T-65 sleep window), so kinematics are ready before the pipeline runs. If Statcast fails, the monitor crashes and `/optimize` returns 503 rather than serving a lineup built on NULL kinematics. For new call-ups with no Savant row yet, the engine routes through a non-Statcast fallback path using the UNKNOWN_SCORE_RATIO baseline.

### Fail-loud data fetches

Every live-data fetch used by the T-65 pipeline either succeeds or fails loud. There are no silent fallbacks.

| Path | On failure |
|---|---|
| Alembic migration (startup) | App lifespan crashes |
| Schema-drift smoke check (startup) | RuntimeError if expected columns or tables are missing |
| Statcast kinematics refresh (in-monitor Phase 2) | Non-zero exit calls `lineup_cache.mark_failed()`, `/optimize` returns 503 |
| Vegas lines (Odds API) | RuntimeError if key missing, quota exhausted, or any game without lines |
| Weather (Open-Meteo) | Raises if any game's weather cannot be fetched |
| Series context + rest days | Raises if any team's schedule lookback fails |
| Team batting/pitching stats | Raises if any field is NULL post-fetch |
| RotoWire expected lineups | Best-effort only. Failure logs but does not abort. The only intentional graceful-degradation path. |

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
- **SQLAlchemy** - ORM with SQLite (swappable to Postgres via `BO_DATABASE_URL`)
- **Alembic** - Schema migrations
- **Redis** - Required cache layer. Startup fails without it.
- **Pydantic** - Request/response validation
- **httpx** - Async HTTP client for MLB Stats API
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
+-- historical_players.csv           # Master player ledger (33 slates, 1221 rows)
+-- historical_winning_drafts.csv    # Top-ranked lineups, 5 slots per lineup
+-- historical_slate_results.json    # Per-date slate envelope (game results, context)
+-- hv_player_game_stats.csv         # Box scores for Highest Value players
```

Current coverage: 2026-03-25 through 2026-04-26 (33 slates). All four files stay in lockstep.

---

## Getting Started

```bash
# Install dependencies
pip install -e ".[dev]"

# Copy and fill in environment variables
cp .env.example .env

# Run the server (DB schema is created via Alembic on startup)
uvicorn app.main:app --reload

# Run tests
pytest tests/
```

### Claude Code cloud

`scripts/cloud-setup.sh` is a reference copy of the bootstrap script that runs when the Claude Code cloud container is provisioned. The canonical runtime lives in the Claude Code cloud environment "Setup script" field. Paste the contents of `scripts/cloud-setup.sh` there to restore or rebuild the environment. The repo copy does not auto-execute.

The script requires `GITHUB_PAT` in cloud secrets (for `gh` CLI and `git push` auth) plus the `BO_*` env vars listed in `.env.example`.

---

## Ingesting a New Slate

New slates are added manually after each slate completes. There is no automated collector. Append rows to the four files in `/data/` (see CLAUDE.md "Ingesting New Slate Data" for the full column-by-column reference and platform-to-CSV mapping). Short version:

1. Append player rows to `historical_players.csv` (Most Popular + Most Drafted 3x mandatory, HV optional).
2. Append winning-lineup rows to `historical_winning_drafts.csv` (5 rows per lineup, target top-20 ranks).
3. Append one slate envelope object to `historical_slate_results.json`.
4. Append HV box-score rows to `hv_player_game_stats.csv`.
5. Verify `total_value = real_score x (2 + card_boost)` for each player row.
6. Run `python scripts/validate_ingest.py --date YYYY-MM-DD` to confirm all four files are in lockstep.

The database does not store historical data. Appending to the four files in `/data/` is the entire ingest.

---

## Deployment (Railway)

The app includes a `Dockerfile` and `Procfile` for Railway deployment. Set `BO_DATABASE_URL` and `PORT` as environment variables. The database schema is created via Alembic on first startup. Only current-cycle live state is persisted.

---

## License

Private project.
