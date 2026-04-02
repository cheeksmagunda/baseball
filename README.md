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

### Three-Stage Pipeline

1. **Collect** — Fetch today's MLB schedule and player stats from the MLB Stats API
2. **Score** — Rate each player 0-100 via trait-based profiling (pitchers: 5 traits, batters: 7 traits)
3. **Optimize** — Assign top cards to slots using the rearrangement inequality (greedy = provably optimal)

### Philosophy

It's not a machine learning model nor a deterministic predictive engine — it's a **rule-based scoring engine** backed by live API data, with a **feedback loop** that gets smarter over time. The goal at all times is to **win the drafts**. We don't need to predict Real Score — we estimate it via player profiling and optimize around card boosts.

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

### Score → RS Mapping
| Score | Estimated RS Range |
|---|---|
| 80-100 | 4.0 – 6.0 |
| 60-79 | 2.5 – 4.0 |
| 40-59 | 1.5 – 2.5 |
| 20-39 | 0.5 – 1.5 |
| 0-19 | -0.5 – 0.5 |

## Strategy Insights

- **Winning formula**: All 5 RS ≥ 1.0 with 2+ RS ≥ 3.0
- **Anti-popularity edge**: Low-ownership players consistently outperform
- **Card boost leverage**: A +3.0x boosted player with RS 2.4 matches an unboosted ace with RS 6.0
- **Draft optimizer**: Rearrangement inequality — highest expected value → highest slot multiplier

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
| POST | `/api/draft/optimize` | Optimal 5-player lineup |
| POST | `/api/draft/evaluate` | Evaluate a proposed lineup |

### Calibration
| Method | Path | Description |
|---|---|---|
| POST | `/api/calibration/{date}` | Calibrate predictions vs actuals |
| GET | `/api/calibration/history` | View calibration history |
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
    ├── scoring_engine.py   # THE HEART — trait-based scorer
    ├── draft_optimizer.py  # Rearrangement inequality optimizer
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
