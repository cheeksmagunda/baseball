# Ben Oracle

A rule-based scoring engine and draft optimizer for **Real Sports DFS** (baseball), backed by live MLB API data.

> **This is NOT traditional DFS.** In Real Sports, there is no salary cap. You draft 5 players into 5 slots with fixed multipliers. Each player has a **card boost** (0 to +3.0x). The core formula:
>
> ```
> total_value = real_score × (2 + card_boost)
> lineup_score = Σ real_score × (slot_mult + card_boost)
> ```
>
> Slot multipliers: 2.0, 1.8, 1.6, 1.4, 1.2 (fixed, not selectable).

## Architecture

### T-65 Event-Driven Timing

The core design principle: **One pipeline run at exactly T-65 (65 minutes before first pitch), then picks are locked.**

```
App Startup (pre-T-65)        T-65 Lock                   T-60 Unlock
     │                          │                            │
     ├─ Load cache              ├─ Fetch MLB data            ├─ Serve picks
     ├─ Start monitor           ├─ Score players             ├─ Users draft
     └─ Sleep (0 API calls)     ├─ Optimize lineups          │
                                ├─ Freeze cache              └─ Monitor completion
                                └─ Lock picks
```

**This ensures:**
- Fresh data (MLB schedule, game conditions) at the moment of line-locking
- No mid-slate interference (e.g., a dyno restart) changes picks
- No fallback to stale data — if T-65 pipeline fails, `/optimize` returns an error
- Zero API activity outside the T-65 window

See CLAUDE.md § "T-65 Sniper Architecture" for complete timing details.

### Four-Stage Pipeline (Runs Once at T-65)

1. **Collect** (`app/services/data_collection.py`) — Fetch fresh MLB schedule, player stats, game context, Vegas lines, and **RotoWire expected lineups** for batting-order enrichment (see V10.3 below)
2. **Score** (`app/services/scoring_engine.py`) — Rate each player 0-100 via trait-based profiling (pitchers: 5 traits, batters: 7 traits)
3. **Filter** (`app/services/filter_strategy.py`) — Apply V12.2 strategy: env scoring rebuilt from a 35-slate quartile audit against actual HV outcomes; only audit-validated signals are scored (opp ERA, opp WHIP, wind speed, park HR, ML mild-fav peak for pitchers, ML underdog premium for batters, etc.). Trait scoring rebalanced to remove env double-counting. Pitcher env ceiling 1.40, batter ceiling 1.30, env floor 0.20 for signal discrimination.
4. **Optimize** (`app/routers/filter_strategy.py` → `run_filter_strategy`) — Build variants 0P+5B / 1P+4B / 2P+3B / 3P+2B / 4P+1B / 5P+0B, return the highest slot-weighted EV. Slot 1 (2.0×) goes to highest-EV player regardless of position. Freeze in cache.

The primary optimization path is `filter_strategy`. The `/api/pipeline/*` manual endpoints exist for post-slate testing only and are gated to prevent mid-slate interference.

### Philosophy

It's not a machine learning model — it's a **rule-based scoring engine** backed by live API data. The goal is to **win drafts**, not predict Real Score. RS is opaque — the optimizer ranks players by pre-game conditions (env_factor) and Statcast-driven traits (trait_factor). V12 rebuilt env scoring from a 35-slate audit against actual HV outcomes — every dead or inverted signal got deleted, only audit-validated signals are scored. The model is popularity-agnostic: don't favor popular players, don't fade unpopular ones either. Multi-pitcher variants (0P-5P) replaced the V11 0P/1P-only constraint that was structurally barring 57% of empirical winning shapes. Slot 1 goes to the highest-EV player regardless of position. **Historical stats are reference data only — they never feed the live scoring pipeline.**

Stacking (multiple batters from the same team) is powerful but correlated. V10.2 unlocks a **mini-stack** (cap 2 per team, cap 2 per game) via two paths:
- **PATH 1** — `moneyline ≤ -200` AND `vegas_total ≥ 9.0` (favored side only, earns +20% STACK_BONUS)
- **PATH 2** — `vegas_total ≥ 10.5` (both sides eligible, no extra bonus — already a high-run game)

Every other team is capped at one batter per lineup. See `is_stack_eligible_game()` in `app/core/constants.py`.

### Pre-Card Lineup Harvesting (V10.3 — RotoWire integration)

The MLB Stats API only exposes lineup cards 30-60 min before first pitch — typically *after* the T-65 lock. Without external data, ~95% of batters at T-65 would have NULL `batting_order` and fall into the DNP_UNKNOWN_PENALTY (0.85), neutralising the Group B "lineup_position" signal across the entire pool.

V10.3 scrapes RotoWire's daily-lineups page (the de-facto source for every open-source MLB DFS optimizer — there is no free first-party API) and pre-fills `SlatePlayer.batting_order` from beat-reporter projections up to 4 hours before first pitch. The official MLB API boxscore overrides RotoWire as ground truth when posted; `batting_order_source` records provenance (`"rotowire_confirmed"` / `"rotowire_expected"` / `"official"`). RotoWire failures are best-effort — they log loudly but don't crash the pipeline (graceful degradation, not a forbidden fallback).

See `app/core/rotowire.py` for the parser and `app/services/data_collection.py::_enrich_batting_order_from_rotowire` for wiring.

## Scoring Engine

### Pitcher Traits (V12.2 — env double-counters zeroed; weights sum to 100)
| Trait | Weight | What It Measures |
|---|---|---|
| ace_status | 30 | ERA-based rotation rank proxy |
| k_rate | 35 | **Statcast (FB velo / IVB / extension / whiff% / chase% — 70%) + K/9 (30%)**. ±5% catcher-framing scaling (TruMedia: 3.9% K-rate per framing run; conservative for 2026 ABS era). |
| matchup_quality | 0 | (V12.2 zeroed — env_factor handles opp OPS / K%, double-count) |
| recent_form | 20 | Last 3 starts quality |
| era_whip | 15 | live ERA (45%) + WHIP (30%) + xERA (25%) |

### Batter Traits (V12.2 — env double-counters zeroed; weights sum to 100)
| Trait | Weight | What It Measures |
|---|---|---|
| power_profile | 40 | avg EV (7) / hard-hit% (7) / barrel% (6) / xwOBA (4) / max EV (1) — boosted from 25 to absorb deleted matchup/park weight |
| matchup_quality | 0 | (V12.2 zeroed — env_factor handles opp ERA / WHIP, double-count) |
| lineup_position | 0 | (V12.2 zeroed — env_factor handles batting_order, double-count) |
| recent_form | 25 | Last 7 games production |
| ballpark_factor | 0 | (V12.2 zeroed — env_factor handles park HR / wind / temp, double-count) |
| hot_streak | 25 | Multi-hit games in last 3 |
| speed_component | 10 | Stolen base pace |

The scoring engine outputs a **0-100 ranking signal**, not an RS prediction.

**Statcast kinematics** (exit velocity, barrel %, hard-hit %, FB velocity, induced vertical break, extension, whiff %, chase %) — plus **V10.8 expected stats** (xwOBA, xBA, xSLG for batters; xERA, xwOBA-against for pitchers) and **V10.8 team catcher framing** — are populated by `scripts/refresh_statcast.py`. The slate monitor awaits this script BLOCKING inside Phase 2 (before the T-65 sleep) so the pipeline never reads pre-refresh PlayerStats with NULL kinematics. Failure raises and the monitor crashes — `/optimize` returns 503 instead of serving lineups built on stale Statcast data. The T-65 pipeline then reads the columns straight from the DB with zero Savant calls. No fixed cron, no Railway UI config — merge and deploy, the next slate fires the refresh on its own. When no Savant row exists yet (new call-ups pre-50 BBE), the engine transparently falls back to the non-Statcast path; true zero-data rookies (MLB debuts) receive the UNKNOWN_SCORE_RATIO baseline so env + park can still promote them (strategy doc §"Rookie Variance Void").

### Fail-loud T-65 data fetch (V10.8 audit)

Every live-data fetch path used by the T-65 pipeline either succeeds and produces data, or fails loud — there is no silent fallback that lets the pipeline produce lineups built on partial data. The fail modes:

| Path | Failure → behaviour |
|---|---|
| Alembic migration (startup) | App lifespan crashes; Railway restarts and operator sees the failure |
| V10.8 schema-drift smoke check (startup, in `app/main.py`) | `RuntimeError` if expected `player_stats` / `slate_games` columns or `team_season_stats` table are missing — catches a bad migration before the slate monitor is even spawned |
| Statcast kinematics + xStats refresh (in-monitor Phase 2) | `scripts/refresh_statcast.py` exits non-zero on schema drift, network failure, or zero rows updated → `_refresh_statcast_background` calls `lineup_cache.mark_failed()` → `/optimize` returns 503 |
| Team catcher framing scrape (V10.8) | Direct Savant CSV/JSON scrape; HTTP error or page-schema change raises and bubbles up to `main()` exit-1 → same mark-failed path. Zero updates is acceptable (early-season pre-data) |
| Vegas lines (Odds API) | `enrich_slate_game_vegas_lines` raises if `BO_ODDS_API_KEY` missing, quota exhausted, or any game without lines — pipeline aborts, `/optimize` returns 503 |
| Weather (Open-Meteo) | `enrich_slate_game_weather` raises if any game's weather can't be fetched |
| Series context + L10 + V10.8 opp-rest-days | `enrich_slate_game_series_context` raises if any team's schedule lookback fails. Date parsing for rest-days has no try/except — a malformed MLB ISO date propagates |
| Team batting / pitching stats | `enrich_slate_game_team_stats` raises if any field NULL post-fetch |
| RotoWire expected lineups (V10.4) | Best-effort — failure logs loudly but doesn't abort. Missing batting orders fall through to `DNP_UNKNOWN_PENALTY` (0.93 in V10.6). The only intentional graceful-degradation path |

League-average defaults (ERA, WHIP, OPS, K%) and all scaling thresholds are centralized in `app/core/constants.py`. Env-score functions use shared `graduated_scale()` helpers in `app/core/utils.py`.

## Optimizer (V12.2)

Single lineup, multi-pitcher variants (0P-5P), audit-validated env signals.
Slot 1 (2.0×) goes to the highest-EV PLAYER regardless of position.

**EV formula:**

```
filter_ev = env_factor × volatility_amplifier × trait_factor × stack_bonus × dnp_adj × 100
```

| Signal | Range | Role |
|---|---|---|
| env_factor (pitchers) | 0.20–1.40 | Primary — ML mild-fav peak, Vegas O/U inverse, park HR, K/9, ERA tail, opp OPS |
| env_factor (batters) | 0.20–1.30 | Primary — opp ERA (strongest), opp WHIP, wind speed, park, ML underdog premium, batting order, temp, platoon |
| volatility_amplifier | 0.8–1.2 (batters only) | env-CONDITIONAL: high-CV hitters get +20% in good env, −20% in bad env |
| trait_factor | 0.85–1.15 | Secondary — intrinsic player talent (Statcast power_profile / k_rate), independent of env |
| stack_bonus | 1.0 or 1.20 | PATH 1 blowout-favorite bonus (gated) |
| dnp_adj | 0.70 / 0.93 / 1.00 | Confirmed-bad / unknown / known batting order |

**Composition** (multi-pitcher variant chooser):
For each pitcher count 0..5: pick top-N pitchers + top-(5-N) batters by EV, slot-weight them all by sorting EV-descending and assigning multipliers 2.0 → 1.2. Return the variant with the highest slot-weighted total. Tiebreak: higher pitcher count wins.

**Anti-correlation guard:** A batter is blocked from any game where one of our drafted pitchers plays UNLESS the batter is that pitcher's teammate. Per-team batter cap (1 default, 2 stack-eligible) and per-game cap (2) still apply.

## Strategy Insights — what 33 slates of data tell us

- **Two-path stacking captures the highest-leverage games.** PATH 2 shootouts (O/U ≥ 10.5) yield 2.31 HV per game vs 1.22 baseline (+89%); PATH 1 blowouts yield 1.50 HV per game (+23%). The mini-stack cap of 2 captures the correlation edge without committing too much of the lineup to one game.
- **Mild-favorite pitchers crush heavy-favorite pitchers (V10.7 fix).** Bucketing pitcher's-team ML by quartile: heavy fav (-310 to -168) mean_rs **3.12** / HV-rate **12.7%**; mild fav (-164 to -120) mean_rs **4.20** / HV-rate **38.2%**. Heavy favorites generate blowouts where the starter gets pulled in the 5th-6th inning before K/win-bonus stack up. V10.7 tightened `PITCHER_ENV_ML_CEILING` from -220 to -150 so the curve saturates at the mild-fav peak.
- **Team momentum signals are INVERTED at the individual-RS level (V10.7 fix).** The L10-wins bucketing showed cold teams (0-4 wins) mean_rs **2.86** vs hot teams (7-10 wins) **2.40** — the opposite of the V10.2 assumption. Same inversion on series lead. Likely mechanism: hot/leading teams have multiple contributors so HV is spread thin; cold/trailing teams have one star carrying the offense. V10.7 neutralised both signals (set bonuses/penalties to 0.0) rather than reversing them — conservative call given the 33-slate sample.
- **Vegas O/U is barely a player-level signal (V10.8 down-weight).** Bucketing 983 batter-slates by O/U: Q1 (6.5-7.5) mean_rs 2.35; Q4 (9.0+) mean_rs 2.62 — a 1.04× swing, barely above noise. Treated as PRIMARY pre-V10.8 (full 1.0 weight in Group A) alongside ERA/ML/bullpen; V10.8 dropped to 0.5 (matching WHIP). Logic: O/U bakes in BOTH teams' offenses; direct opp-pitcher signals (ERA, WHIP, K/9) carry the matchup-specific information.
- **Pitchers were saturating the top-10 (V10.6 fix).** An offline harness against the same 33-slate corpus showed 54% of model top-10 were pitchers (target ~40% given ~50% of HV slots historically go to batters). Tightening the pitcher env ceiling to 1.20× and refining DNP_UNKNOWN_PENALTY brought pitcher share to 39.7%.
- **V10.8 sustainable signal expansion**: added Statcast xStats (xwOBA / xERA), opp-arsenal-effectiveness via xwOBA-against, conservative ±5% catcher framing scaling, and opponent rest days. xStats are research-backed industry-standard predictive metrics; framing is throttled for the 2026 ABS Challenge System era; opp rest days substitutes for own-rest-days (which FanGraphs research shows has no edge for starters).
- **Cumulative eval impact**: V10.5 baseline HV@20 = 8.24/20. V10.6 → 8.73/20. V10.7 → 9.15/20. **V10.8 → 9.18/20** (above the 8-9 target). The V10.8 lift is small in eval because historicals don't yet have the new fields populated; live impact will be larger once the refresh script populates xStats/framing on the next slate cycle.
- **Slot sequencing**: Slot 1 is always the anchor pitcher (when present). Among batters in Slots 2–5, picks are sorted by `filter_ev` descending — the highest-EV batter takes Slot 2 (1.8×), tail batters fill the lower slots.

> **Strategy details by version:** see CLAUDE.md § "V10.8 Sustainable Signal Expansion", "V10.7 Fresh-Eyes Feature Audit", "V10.6 Pitcher-Batter Parity", "V10.5 EV-Driven Composition + Bifurcated FADE", "V10.4 Pre-Card Lineup Harvesting + Decoupled Batter ML", "V10.3 Calibration", "V10.2 Calibration Changes", "V10.1 Structural Changes", "V10.0 Core Architecture".

### V10.8 research citations (DFS literature consulted before adding signals)

- [MLB Glossary on xwOBA](https://www.mlb.com/glossary/statcast/expected-woba)
- [Baseball Savant Expected Statistics Leaderboard](https://baseballsavant.mlb.com/leaderboard/expected_statistics)
- [pybaseball — Statcast wrapper](https://github.com/jldbc/pybaseball)
- [MLB.com — 2026 ABS Challenge System](https://www.mlb.com/news/abs-challenge-system-mlb-2026)
- [TruMedia Catcher Framing model — 3.9% K-rate per framing run](https://baseball.help.trumedianetworks.com/baseball/catcher-framing-model)
- [FanGraphs — Effect of Rest Days on Starting Pitchers (no significant edge)](https://community.fangraphs.com/the-effect-of-rest-days-on-starting-pitcher-performance/)
- [FantasyLabs — Opponent Rest Days as DFS edge](https://www.fantasylabs.com/articles/how-does-a-well-rested-opponent-affect-hitters-and-pitchers/)

## API Endpoints

All endpoints are under `/api/`.

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

### Filter Strategy (Primary Optimizer)
| Method | Path | Description |
|---|---|---|
| GET | `/api/filter-strategy/status` | T-65 countdown, cache state, first-pitch time |
| GET | `/api/filter-strategy/optimize` | Serve the frozen single lineup (V11.0) |
| POST | `/api/filter-strategy/classify-slate` | Classify a slate without running the optimizer |
| GET | `/api/filter-strategy/diagnostics` | Pipeline health dashboard |

### Weights
| Method | Path | Description |
|---|---|---|
| GET | `/api/calibration/weights` | Get current scoring weights |
| PUT | `/api/calibration/weights` | Update scoring weights |

### Pipeline
| Method | Path | Description |
|---|---|---|
| POST | `/api/pipeline/fetch/{date}` | Fetch schedule + stats |
| POST | `/api/pipeline/score/{date}` | Score a slate |
| POST | `/api/pipeline/run/{date}` | Full pipeline (fetch → score) |
| POST | `/api/pipeline/filter-strategy/{date}` | Full 5-filter pipeline (post-slate/testing only) |

## Tech Stack

- **FastAPI** — REST API framework
- **SQLAlchemy** — ORM with SQLite (swappable to Postgres)
- **Alembic** — schema migrations
- **Redis** — required cache layer (startup fails without it; no DB-only fallback)
- **Pydantic** — Request/response validation
- **httpx** — Async MLB Stats API client
- **NumPy** — Calibration metrics
- **Railway** — Deployment target (single-replica only — the T-65 monitor is in-process singleton state)

## Project Structure

```
app/
├── main.py                 # FastAPI app + CORS + lifespan
├── config.py               # pydantic-settings (BO_ prefix)
├── database.py             # SQLAlchemy engine + session
├── seed.py                 # Historical data loader
├── core/
│   ├── constants.py        # Slot multipliers, RS ranges, park factors, all thresholds
│   ├── weights.py          # Configurable scoring weights
│   ├── utils.py            # Shared formulas (compute_total_value, scale_score, etc.)
│   ├── mlb_api.py          # MLB Stats API client
│   ├── odds_api.py         # The Odds API client (Vegas moneyline + O/U)
│   ├── open_meteo.py       # Weather API client (temperature, wind)
│   ├── statcast.py         # Baseball Savant kinematics (FB velo, IVB, exit velo, barrel%)
│   └── rotowire.py         # RotoWire daily-lineups parser (V10.3 expected lineups)
├── models/
│   ├── player.py           # Player, PlayerStats, PlayerGameLog, TeamSeasonStats (V10.8)
│   ├── slate.py            # Slate, SlateGame, SlatePlayer
│   ├── scoring.py          # PlayerScore, ScoreBreakdown
│   ├── draft.py            # DraftLineup, DraftSlot
│   └── calibration.py      # WeightHistory
├── schemas/                # Pydantic request/response models
├── routers/                # API route handlers
└── services/
    ├── scoring_engine.py   # Trait-based scorer (0-100)
    ├── filter_strategy.py  # THE HEART — EV pipeline + single-lineup optimizer (V11.0)
    ├── candidate_resolver.py  # Builds FilteredCandidate pool from DB (batched lookups)
    ├── lineup_cache.py     # Frozen-cache invariants (Redis + SQLite persistence)
    ├── slate_monitor.py    # T-65 event loop
    ├── data_collection.py  # MLB API + RotoWire data fetching
    └── pipeline.py         # Fetch → Score → Rank orchestrator
data/
├── historical_players.csv           # 1221 rows / 33 dates — master player ledger
├── historical_winning_drafts.csv    # ~1100 rows / 33 dates — top-ranked lineups (5 slots/lineup)
├── historical_slate_results.json    # 33 entries           — per-date slate envelope
└── hv_player_game_stats.csv         # ~500 rows / 33 dates — box scores for HV players
```

Current coverage: 2026-03-25 → 2026-04-26 (33 slates). All four files stay in lockstep.

## Getting Started

```bash
# Install dependencies
pip install -e ".[dev]"

# Set environment variables (or copy .env.example → .env)
export BO_DATABASE_URL=sqlite:///db/ben_oracle.db

# Seed historical data
python -m app.seed

# Run the server
uvicorn app.main:app --reload

# Run tests
pytest tests/
```

### Claude Code cloud

`scripts/cloud-setup.sh` is a **reference copy** of the bootstrap script that runs when the Claude Code cloud container is provisioned. The canonical runtime lives in the Claude Code cloud environment "Setup script" field — paste the contents of `scripts/cloud-setup.sh` there to restore or rebuild the environment. The repo copy does not auto-execute.

The script requires `GITHUB_PAT` in cloud secrets (for `gh` CLI + `git push` auth) plus the `BO_*` env vars listed in `.env.example`.

## Ingesting a New Slate

New slates are added **manually** — there is no automated collector. After a slate completes, append rows to the four files in `/data/` (see the **"Ingesting New Slate Data"** section in [CLAUDE.md](CLAUDE.md) for the full column-by-column reference and platform → CSV mapping). The short version:

1. Append player rows to `historical_players.csv` (Most Popular + Most Drafted 3x mandatory; HV optional).
2. Append winning-lineup rows to `historical_winning_drafts.csv` (5 rows per lineup, target top-20 ranks).
3. Append one slate envelope object to `historical_slate_results.json`.
4. Append HV box-score rows to `hv_player_game_stats.csv`.
5. Verify `total_value = real_score × (2 + card_boost)` for each player row. (Note: `card_boost` is used only for computing historical total_value — never as a scoring/prediction input.)
6. Reload the DB: `rm db/ben_oracle.db && python -m app.seed` (the seeder is idempotency-guarded on an empty DB — there is no incremental mode).

## Deployment (Railway)

The app includes a `Dockerfile` and `Procfile` for Railway deployment. Set `BO_DATABASE_URL` and `PORT` as environment variables. The database is seeded automatically on first startup via the lifespan hook.

## License

Private project.
