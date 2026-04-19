from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BO_")

    database_url: str = f"sqlite:///{Path(__file__).resolve().parent.parent / 'db' / 'ben_oracle.db'}"
    mlb_api_base_url: str = "https://statsapi.mlb.com/api/v1"
    cors_origins: list[str] = ["*"]
    log_level: str = "INFO"
    # REQUIRED: set via BO_CURRENT_SEASON env var. No default — the operator must
    # explicitly pick the active MLB season year. See "MLB Season Calendar" in CLAUDE.md.
    current_season: int
    redis_url: str | None = None

    # The Odds API key for fetching pre-game Vegas lines (moneyline + O/U totals).
    # Free tier: 500 requests/month.  Optional — omitting skips Vegas enrichment with a warning.
    # Reads from BO_ODDS_API_KEY (standard BO_ prefix).
    odds_api_key: str | None = None


settings = Settings()
