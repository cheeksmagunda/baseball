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

1. **Collect** (`app/services/data_collection.py`) — Fetch fresh MLB schedule, player stats, game context, Vegas lines
2. **Score** (`app/services/scoring_engine.py`) — Rate each player 0-100 via trait-based profiling (pitchers: 5 traits, batters: 7 traits)
3. **Filter** (`app/services/filter_strategy.py`) — Apply V10.1 strategy: exclude FADE players, score env/trait EV (with Statcast kinematics), enforce composition rules with conditional stacking
4. **Optimize** (`app/routers/filter_strategy.py` → `run_dual_filter_strategy`) — Produce Starting 5 + Moonshot lineups, freeze in cache

The primary optimization path is `filter_strategy`. The `/api/pipeline/*` manual endpoints exist for post-slate testing only and are gated to prevent mid-slate interference.

### Philosophy

It's not a machine learning model — it's a **rule-based scoring engine** backed by live API data. The goal is to **win drafts**, not predict Real Score. RS is opaque — the optimizer ranks players by pre-game conditions (env_factor) and Statcast-driven traits (trait_factor), then excludes high-media-attention players (FADE gate) before selecting the lineup. **Historical stats are reference data only — they never feed the live scoring pipeline directly.**

Stacking (multiple batters from the same team) is powerful but correlated. V10.1 only unlocks a **mini-stack** (cap 2 per team, cap 2 per game) when a team plays in a game with BOTH `moneyline ≤ -200` AND `vegas_total ≥ 9.0` — see `is_stack_eligible_game()` in `app/core/constants.py`. Every other team is capped at one batter per lineup.

## Scoring Engine

### Pitcher Traits (5 traits, 0-100 total)
| Trait | Default Weight | What It Measures |
|---|---|---|
| ace_status | 25 | ERA-based rotation rank proxy |
| k_rate | 25 | **Statcast (FB velo / IVB / extension / whiff% / chase% — 70% weight) + K/9 (30%)** |
| matchup_quality | 20 | Opponent offensive weakness |
| recent_form | 15 | Last 3 starts quality |
| era_whip | 15 | Combined ERA + WHIP |

### Batter Traits (7 traits, 0-100 total)
| Trait | Default Weight | What It Measures |
|---|---|---|
| power_profile | 25 | **avg EV (8 pts) / hard-hit% (7) / barrel% (6) / max EV (2) / HR/PA (2)** |
| matchup_quality | 20 | Opposing pitcher weakness |
| lineup_position | 15 | Batting order (slots 1-4 = equal max) |
| recent_form | 15 | Last 7 games production |
| ballpark_factor | 10 | Home park HR factor |
| hot_streak | 10 | Multi-hit games in last 3 |
| speed_component | 5 | Stolen base pace |

The scoring engine outputs a **0-100 ranking signal**, not an RS prediction.

**Statcast kinematics** (exit velocity, barrel %, hard-hit %, FB velocity, induced vertical break, extension, whiff %, chase %) are populated overnight by `scripts/refresh_statcast.py` (run it as a daily cron — `0 3 * * *`). The T-65 pipeline reads them from the DB with zero Baseball Savant calls, which keeps the lock window safe from Savant rate-limits. When no Savant row exists yet (new call-ups pre-50 BBE), the engine transparently falls back to the non-Statcast path; true zero-data rookies (MLB debuts with no stats at all) receive the UNKNOWN_SCORE_RATIO baseline on power_profile / k_rate so env + popularity + park can still promote them (strategy doc §"Rookie Variance Void").

**Important:** `card_boost` is revealed during/after the draft and is **structurally absent from `FilteredCandidate`** — the optimizer cannot read it even by accident. League-average defaults (ERA, WHIP, OPS, K%) and all scaling thresholds are centralized in `app/core/constants.py`. Env-score functions use shared `graduated_scale()` helpers in `app/core/utils.py`.

## Popularity Signal Aggregator (Pre-Game Signals Only)

The optimizer automatically fades over-hyped players and targets under-the-radar picks using **pre-game public signals only**:

| Source | Weight | What It Measures |
|---|---|---|
| Social trending | 45% | Google Trends — is the casual public talking about this player? |
| Sports news | 25% | ESPN/MLB.com RSS — is this player in headlines? |
| Search volume | 30% | Google autocomplete — casual search interest |

**Intentionally excluded:** RotoGrinders/NumberFire platform ownership — this is only visible during the draft and violates the pre-game signals constraint.

**Classification:** FADE, TARGET, or NEUTRAL. FADE players are **excluded from the candidate pool before EV runs** — they never reach the optimizer. TARGET and NEUTRAL players pass the gate and are scored identically by env/trait EV (no popularity multiplier). Target batters average RS 3.57 with 73.6% Highest-Value rate. Fade batters average RS 0.98 with 9.6% HV rate — a **3.6× differential** that makes the exclusion gate the single most impactful pre-game filter. See CLAUDE.md § "V9.0 Core Architecture" for the full EV formula.

### Sharp Signal (Underground)

A fifth signal source used exclusively by the Moonshot lineup:

| Source | What It Scrapes |
|---|---|
| r/fantasybaseball, r/baseball | Reddit JSON API — niche community buzz |
| FanGraphs community blogs | RSS feed — deep-dive analyst chatter |
| Prospects Live | RSS feed — prospect/breakout coverage |

If small, smart accounts are on a player but ESPN isn't — that's a Moonshot BUY. Sharp score (0-100) gives up to +35% EV boost in Moonshot only.

## Dual-Lineup Optimizer (V9.0 — Popularity Gate + Env/Trait EV)

The optimizer produces **two lineups** from the same ranked candidate pool. Each lineup is structurally fixed at **exactly 1 starting pitcher + 4 batters**, with the pitcher pinned to Slot 1 (2.0x multiplier):

| Lineup | Structure | Strategy | Popularity handling | Edge |
|---|---|---|---|---|
| **Starting 5** | 1 SP (Slot 1) + 4 batters (Slots 2–5) | Best env+trait EV | FADE players **excluded** from pool | Primary win probability |
| **Moonshot** | 1 SP (Slot 1) + 4 batters (Slots 2–5) | env+trait EV + sharp/explosive bonuses | FADE players **excluded** from pool | Anti-crowd, sharp signal, HR power |

Each lineup's anchor pitcher is the highest-EV pitcher in its candidate pool. The anchor's `game_id` is blocked for batter selection so no batter (teammate or opponent) in that game can appear — no negative correlation between the pitcher and the rest of the lineup.

**V9.0 EV formula (env/trait-only):**

```
base_ev = env_factor × trait_factor × stack_bonus × dnp_adj × 100
```

FADE players (high pre-game media attention) are excluded from the candidate pool before EV runs. The env_factor (0.70–1.30) is the primary differentiator; trait_factor (0.85–1.15) breaks ties within env tiers.

**Key optimizer behaviors:**
- **Pitcher anchor**: exactly 1 SP per lineup, pinned to Slot 1. The highest-EV pitcher in the pool wins the anchor.
- **Game-blocking**: the anchor pitcher's `game_id` is excluded from all batter picks (no batter from the same game).
- **Team/game cap**: max 1 player per team and 1 player per game per lineup.
- Moonshot uses the same FADE-excluded pool but adds sharp signal (+35% max from Reddit/FanGraphs/Prospects Live) and explosive bonus (+20% from power_profile or k_rate).
- **Historical win rate data (draft tier × boost) is used for calibration reference only — not as a live EV input.**

## Strategy Insights

- **Primary edge**: Ghost+high-boost (< 100 drafts, boost ≥ 2.5) wins 82–100% of the time historically. Picking this tier correctly matters more than any trait score.
- **Boost trap**: Medium/chalk-draft players with high boost (200–1499 drafts, boost ≥ 2.0) win only 0–12% of the time. The crowd sees the boost and piles in, but RS doesn't follow.
- **Card boost math**: A ghost with RS 3.0 and +3.0x boost (TV 15.0) decisively beats an unboosted chalk player with RS 5.0 (TV 10.0).
- **Slot sequencing (V5.0)**: Slot 1 is always the anchor pitcher. Among batters in Slots 2–5, unboosted batters take the highest available slot (Slot 2 first) because of the 67% value loss from Slot 1→5; boosted batters tail into the lower slots (only 16% loss at +3.0x).

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
| GET | `/api/filter-strategy/optimize` | Serve frozen Starting 5 + Moonshot picks |
| POST | `/api/filter-strategy/classify-slate` | Classify a slate without running the optimizer |
| GET | `/api/filter-strategy/diagnostics` | Pipeline health dashboard |

### Draft
| Method | Path | Description |
|---|---|---|
| POST | `/api/draft/evaluate` | Evaluate a proposed lineup |

### Popularity
| Method | Path | Description |
|---|---|---|
| POST | `/api/popularity/player` | Check popularity signals for a player |
| POST | `/api/popularity/slate/{date}` | Popularity analysis for entire slate |

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
│   └── open_meteo.py       # Weather API client (temperature, wind)
├── models/
│   ├── player.py           # Player, PlayerStats, PlayerGameLog
│   ├── slate.py            # Slate, SlateGame, SlatePlayer
│   ├── scoring.py          # PlayerScore, ScoreBreakdown
│   ├── draft.py            # DraftLineup, DraftSlot
│   └── calibration.py      # WeightHistory
├── schemas/                # Pydantic request/response models
├── routers/                # API route handlers
└── services/
    ├── scoring_engine.py   # Trait-based scorer (0-100)
    ├── filter_strategy.py  # THE HEART — EV pipeline + dual-lineup optimizer (Starting 5 + Moonshot)
    ├── candidate_resolver.py  # Builds FilteredCandidate pool from DB (batched lookups)
    ├── draft_optimizer.py  # User-proposed-lineup evaluator (/api/draft/evaluate only)
    ├── lineup_cache.py     # Frozen-cache invariants (Redis + SQLite persistence)
    ├── slate_monitor.py    # T-65 event loop
    ├── popularity.py       # Web-scraping popularity signal aggregator
    ├── data_collection.py  # MLB API data fetching
    ├── pipeline.py         # Fetch → Score → Rank orchestrator
    └── condition_classifier.py  # Meta-game monitoring (entropy, Gini)
data/
├── historical_players.csv           # 904 rows / 25 dates — master player ledger
├── historical_winning_drafts.csv    # 910 rows / 25 dates — top-ranked lineups (5 slots/lineup)
├── historical_slate_results.json    # 25 entries            — per-date slate envelope
└── hv_player_game_stats.csv         # 396 rows / 25 dates — box scores for HV players
```

Current coverage: 2026-03-25 → 2026-04-18 (25 slates). All four files stay in lockstep.

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
