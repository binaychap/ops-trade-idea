"""Main application environment settings."""
from typing import Literal

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from app.config.strategy import StrategyExitSettings


class Settings(StrategyExitSettings):
    """Runtime settings loaded from environment variables."""
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    max_notional_usd: float = Field(default=250.0, gt=0.0, alias="MAX_NOTIONAL_USD")
    allow_short_selling: bool = Field(default=False, alias="ALLOW_SHORT_SELLING")
    force_reprocess: bool = Field(default=False, alias="FORCE_REPROCESS")
    optionomics_api_key: str | None = Field(default=None, alias="OPTIONOMICS_API_KEY")
    optionomics_email: str | None = Field(default=None, alias="OPTIONOMICS_EMAIL")
    optionomics_api_url: str = Field(default="https://optionomics.ai/api/v1/trade_ideas", alias="OPTIONOMICS_API_URL")
    optionomics_poll_enabled: bool = Field(default=True, alias="OPTIONOMICS_POLL_ENABLED")
    optionomics_poll_interval_seconds: int = Field(default=600, ge=1, alias="OPTIONOMICS_POLL_INTERVAL_SECONDS")

    # Webull credentials live on WebullSettings (inherited via StrategyExitSettings):
    # WEBULL_APP_KEY/SECRET, WEBULL_PAPER_APP_KEY/SECRET, WEBULL_LIVE_APP_KEY/SECRET.

    next_day_exit_enabled: bool = Field(default=False, alias="NEXT_DAY_EXIT_ENABLED")
    next_day_exit_time: str = Field(default="09:35", pattern=r"^(09:(3[0-9]|[45][0-9])|1[0-5]:[0-5][0-9])$", alias="NEXT_DAY_EXIT_TIME")
    next_day_exit_timezone: Literal["America/New_York"] = Field(default="America/New_York", alias="NEXT_DAY_EXIT_TIMEZONE")
    next_day_exit_poll_seconds: int = Field(default=30, ge=10, alias="NEXT_DAY_EXIT_POLL_SECONDS")

    morning_sell_enabled: bool = Field(default=False, alias="MORNING_SELL_ENABLED")
    morning_sell_time: str = Field(default="10:00", pattern=r"^(09:(3[0-9]|[45][0-9])|1[0-5]:[0-5][0-9])$", alias="MORNING_SELL_TIME")
    morning_sell_timezone: Literal["America/New_York"] = Field(default="America/New_York", alias="MORNING_SELL_TIMEZONE")
    morning_sell_poll_seconds: int = Field(default=60, ge=10, alias="MORNING_SELL_POLL_SECONDS")

    database_path: str = Field(default="bot.sqlite3", alias="DATABASE_PATH")
    # Bearer token for the iOS trading API. Empty disables /api/trading/*.
    ios_api_key: str = Field(default="", alias="IOS_API_KEY", repr=False)

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


