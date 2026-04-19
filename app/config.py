from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BO_", extra="ignore")

    database_url: str = f"sqlite:///{Path(__file__).resolve().parent.parent / 'db' / 'ben_oracle.db'}"
    mlb_api_base_url: str = "https://statsapi.mlb.com/api/v1"
    cors_origins: list[str] = ["*"]
    log_level: str = "INFO"
    # REQUIRED: set via BO_CURRENT_SEASON env var. No default — the operator must
    # explicitly pick the active MLB season year. See "MLB Season Calendar" in CLAUDE.md.
    current_season: int
    # Redis URL. Primary: BO_REDIS_URL. Railway auto-injects REDIS_PRIVATE_URL and
    # REDIS_URL from the Redis service — accepted as fallbacks so the backend works
    # without manual reference-variable wiring in Railway.
    redis_url: str | None = None

    # The Odds API key for fetching pre-game Vegas lines (moneyline + O/U totals).
    # Free tier: 500 requests/month.  REQUIRED — T-65 pipeline raises RuntimeError if
    # unset or unreachable.  Reads from BO_ODDS_API_KEY (standard BO_ prefix).
    odds_api_key: str | None = None

    def model_post_init(self, __context) -> None:
        # If BO_REDIS_URL was not set, accept Railway's auto-injected Redis vars.
        if self.redis_url is None:
            import os
            self.redis_url = (
                os.environ.get("REDIS_PRIVATE_URL")
                or os.environ.get("REDIS_URL")
            )


settings = Settings()
