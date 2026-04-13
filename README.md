# Baseball DFS Predictor — Real Sports Edition

A rule-based scoring engine and draft optimizer for **Real Sports DFS** (baseball), backed by live MLB API data with a feedback loop that gets smarter over time.

> **This is NOT traditional DFS.** In Real Sports, there is no salary cap. You draft 5 players into 5 slots with fixed multipliers. Each player has a **card boost** (0 to +3.0x). The core formula:
>
> ```
> total_value = real_score × (2 + card_boost)
> lineup_score = Σ real_score × (slot_mult + card_boost)
> ```
>
> Slot multipliers: 2.0, 1.8, 1.6, 1.4, 1.2 (fixed, not selectable).

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌────────────────┐
│  MLB Stats   │────▶│  Scoring     │────▶│  Draft          │
│  API (live)  │     │  Engine      │     │  Optimizer      │
└─────────────┘     │  (0-100)     │     │  (5 slots)      │
                    └──────┬───────┘     └────────┬────────┘
                           │                      │
                    ┌──────▼───────┐     ┌────────▼────────┐
                    │  RS Range    │     │  Rearrangement   │
                    │  Estimation  │     │  Inequality      │
                    └──────────────┘     └─────────────────┘
                           │
                    ┌──────▼───────┐
                    │  Calibration │
                    │  Feedback    │
                    └──────────────┘
```

### Four-Stage Pipeline

1. **Collect** (`app/services/data_collection.py`) — Fetch today's MLB schedule, player stats, and game context from the MLB Stats API
2. **Score** (`app/services/scoring_engine.py`) — Rate each player 0-100 via trait-based profiling (pitchers: 5 traits, batters: 7 traits)
3. **Filter** (`app/services/filter_strategy.py`) — Apply five sequential filters: slate classification, environmental advantage, ownership leverage, boost optimization, slot sequencing
4. **Optimize** (`app/routers/filter_strategy.py` → `run_dual_filter_strategy`) — Produce Starting 5 + Moonshot lineups

The primary optimization path is `filter_strategy`, not `draft_optimizer.py` (which is dead code kept only for lineup evaluation).

### Philosophy

It's not a machine learning model — it's a **rule-based scoring engine** backed by live API data with a feedback loop. The goal is to **win drafts**, not predict Real Score. The optimizer identifies which players are in the ghost+high-boost category, because that category's historical win rate (82%) is itself the signal. Trait scores from the scoring engine are secondary — they matter far less than draft tier × boost.

## Scoring Engine

### Pitcher Traits (5 traits, 0-100 total)
| Trait | Default Weight | What It Measures |
|---|---|---|
| ace_status | 25 | ERA-based rotation rank proxy |
| k_rate | 25 | K/9 strikeout rate |
| matchup_quality | 20 | Opponent offensive weakness |
| recent_form | 15 | Last 3 starts quality |
| era_whip | 15 | Combined ERA + WHIP |

### Batter Traits (7 traits, 0-100 total)
| Trait | Default Weight | What It Measures |
|---|---|---|
| power_profile | 25 | HR rate, barrel%, ISO |
| matchup_quality | 20 | Opposing pitcher weakness |
| lineup_position | 15 | Batting order (2-4 = best) |
| recent_form | 15 | Last 7 games production |
| ballpark_factor | 10 | Home park HR factor |
| hot_streak | 10 | Multi-hit games in last 3 |
| speed_component | 5 | Stolen base pace |

The scoring engine outputs a **0-100 ranking signal**, not an RS prediction. We don't pretend to know Real Score — we rank players by pre-game indicators and let the optimizer do the rest.

## Popularity Signal Aggregator

The optimizer automatically fades over-hyped players and targets under-the-radar picks using real-time web signals:

| Source | Weight | What It Measures |
|---|---|---|
| Social trending | 40% | Google Trends — is the casual public talking about this player? |
| Sports news | 20% | ESPN/MLB.com RSS — is this player in headlines? |
| DFS ownership | 20% | RotoGrinders/NumberFire — cross-platform fantasy ownership |
| Search volume | 20% | Google autocomplete — casual search interest |

**Classification:** FADE (25% EV penalty), TARGET (15% EV bonus), or NEUTRAL. The key insight: "trending" is not the same as "popular." A breakout rookie trending upward is a TARGET. A slumping star trending on ESPN is a FADE.

### Sharp Signal (Underground)

A fifth signal source used exclusively by the Moonshot lineup:

| Source | What It Scrapes |
|---|---|
| r/fantasybaseball, r/baseball | Reddit JSON API — niche community buzz |
| FanGraphs community blogs | RSS feed — deep-dive analyst chatter |
| Prospects Live | RSS feed — prospect/breakout coverage |

If small, smart accounts are on a player but ESPN isn't — that's a Moonshot BUY. Sharp score (0-100) gives up to +25% EV boost in Moonshot only.

## Dual-Lineup Optimizer (V5.0 "Pitcher-Anchor Rule")

The optimizer produces **two lineups** from the same ranked candidate pool. Each lineup is structurally fixed at **exactly 1 starting pitcher + 4 batters**, with the pitcher pinned to Slot 1 (2.0x multiplier):

| Lineup | Structure | Strategy | Popularity | Edge |
|---|---|---|---|---|
| **Starting 5** | 1 SP (Slot 1) + 4 batters (Slots 2–5) | Best EV | FADE=0.75, TARGET=1.15 | Primary win probability |
| **Moonshot** | 1 SP (Slot 1) + 4 batters (Slots 2–5) | Completely different 5 | FADE=0.60, TARGET=1.30 | Anti-crowd, sharp signal, HR power |

Each lineup's anchor pitcher is the highest-EV pitcher in its candidate pool. The anchor's `game_id` is blocked for batter selection so no batter (teammate or opponent) in that game can appear — no negative correlation between the pitcher and the rest of the lineup.

**The primary signal is draft tier × boost**, not trait score:

| Draft tier + boost ≥ 2.0 | Avg TV | % TV>15 (15 dates) |
|---|---|---|
| mega-ghost (<50 drafts) | 19.9 | **82%** |
| ghost (50–99 drafts) | 20.7 | **100%** |
| medium (200–499 drafts) | 2.5 | 0% |
| chalk (1500+ drafts) | 6.4 | 25% |

**Key optimizer behaviors:**
- **V5.0 pitcher anchor**: exactly 1 SP per lineup, pinned to Slot 1. The highest-EV pitcher in the pool wins the anchor — boosted or unboosted, ghost or chalk, treated uniformly.
- **Game-blocking**: the anchor pitcher's `game_id` is excluded from all batter picks (prevents batter-vs-own-pitcher and teammate-of-opposing-pitcher conflicts).
- Top-5 most-drafted boost=3.0 batters flagged each run as chalk+boost traps (57% bust rate; pitchers are exempt per V3.1).
- Mega-ghost+3x players (< 50 drafts) get 1.50× synergy bonus and env penalty cap of 20%
- Ghost+boost EV floor at score=30 (env-independent) prevents data scarcity from burying ghost picks
- Min 1 ghost batter in lineup enforced; fallback accepts mega-ghost+3x even without env data (anchor pitcher is exempt from the swap)
- Moonshot: zero player overlap with Starting 5 (normally forces a different anchor pitcher); heavier anti-crowd lean; underground sharp signal (+25% EV max)

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

### Draft
| Method | Path | Description |
|---|---|---|
| POST | `/api/draft/optimize` | Optimal Starting 5 lineup (popularity-aware) |
| POST | `/api/draft/dual-optimize` | Both Starting 5 + Moonshot lineups (sharp-signal-aware) |
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

## Tech Stack

- **FastAPI** — REST API framework
- **SQLAlchemy** — ORM with SQLite (swappable to Postgres)
- **Pydantic** — Request/response validation
- **httpx** — Async MLB Stats API client
- **NumPy** — Calibration metrics
- **Railway** — Deployment target

## Project Structure

```
app/
├── main.py                 # FastAPI app + CORS + lifespan
├── config.py               # pydantic-settings (DFS_ prefix)
├── database.py             # SQLAlchemy engine + session
├── seed.py                 # Historical data loader
├── core/
│   ├── constants.py        # Slot multipliers, RS ranges, park factors
│   ├── weights.py          # Configurable scoring weights
│   ├── utils.py            # Shared formulas (compute_total_value, etc.)
│   └── mlb_api.py          # MLB Stats API client
├── models/
│   ├── player.py           # Player, PlayerStats, PlayerGameLog
│   ├── slate.py            # Slate, SlateGame, SlatePlayer
│   ├── scoring.py          # PlayerScore, ScoreBreakdown
│   ├── draft.py            # DraftLineup, DraftSlot
│   └── calibration.py      # CalibrationResult, WeightHistory
├── schemas/                # Pydantic request/response models
├── routers/                # API route handlers
└── services/
    ├── scoring_engine.py   # Trait-based scorer (0-100)
    ├── filter_strategy.py  # THE HEART — EV pipeline + dual-lineup optimizer (Starting 5 + Moonshot)
    ├── draft_optimizer.py  # DEAD CODE — kept only for evaluate_lineup; superseded by filter_strategy
    ├── popularity.py       # Web-scraping popularity signal aggregator
    ├── data_collection.py  # MLB API data fetching
    ├── pipeline.py         # Fetch → Score → Rank orchestrator
    └── calibration.py      # Prediction vs actual feedback loop
data/
├── historical_players.csv
├── historical_winning_drafts.csv
├── historical_slate_results.json
└── hv_player_game_stats.csv
```

## Getting Started

```bash
# Install dependencies
pip install -e ".[dev]"

# Set environment variables (or copy .env.example → .env)
export DFS_DATABASE_URL=sqlite:///db/baseball.db

# Seed historical data
python -m app.seed

# Run the server
uvicorn app.main:app --reload

# Run tests
pytest tests/
```

## Deployment (Railway)

The app includes a `Dockerfile` and `Procfile` for Railway deployment. Set `DFS_DATABASE_URL` and `PORT` as environment variables. The database is seeded automatically on first startup via the lifespan hook.

## License

Private project.
