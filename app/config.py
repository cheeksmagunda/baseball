from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DFS_")

    database_url: str = f"sqlite:///{Path(__file__).resolve().parent.parent / 'db' / 'baseball_dfs.db'}"
    mlb_api_base_url: str = "https://statsapi.mlb.com/api/v1"
    cors_origins: list[str] = ["*"]
    log_level: str = "INFO"
    current_season: int = 2026
    redis_url: str | None = None

    # The Odds API key for fetching pre-game Vegas lines (moneyline + O/U totals).
    # Free tier: 500 requests/month.  Optional — omitting skips Vegas enrichment with a warning.
    # Reads from DFS_ODDS_API_KEY (standard DFS_ prefix).
    odds_api_key: str | None = None


settings = Settings()
