"""One environment selection for Webull trading and market data."""
import os
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


ENDPOINTS = {"paper": "api.sandbox.webull.com", "live": "api.webull.com"}


def resolve_webull_endpoint(mode=None, endpoint=None):
    mode = os.getenv("WEBULL_TRADING_MODE", "paper") if mode is None else mode
    endpoint = os.getenv("WEBULL_ENDPOINT", "") if endpoint is None else endpoint
    if mode not in ENDPOINTS:
        raise ValueError("WEBULL_TRADING_MODE must be paper or live")
    expected = ENDPOINTS[mode]
    if endpoint and endpoint != expected:
        raise ValueError(f"WEBULL_ENDPOINT must be {expected} for WEBULL_TRADING_MODE={mode}")
    return expected


def resolve_webull_credentials(settings=None):
    """Live requires explicit production credentials; legacy keys are paper-only."""
    def value(name):
        if settings is not None:
            return getattr(settings, name.lower(), None)
        return os.getenv(name)

    mode = value("WEBULL_TRADING_MODE") or "paper"
    if mode not in ENDPOINTS:
        raise ValueError("WEBULL_TRADING_MODE must be paper or live")
    prefix = f"WEBULL_{mode.upper()}"
    key, secret = value(prefix + "_APP_KEY"), value(prefix + "_APP_SECRET")
    if mode == "paper" and not key and not secret:
        key, secret = value("WEBULL_APP_KEY"), value("WEBULL_APP_SECRET")
    if not key or not secret:
        raise ValueError(f"Configure {prefix}_APP_KEY and {prefix}_APP_SECRET")
    return key, secret


class WebullSettings(BaseSettings):
    webull_app_key: str | None = Field(default=None, alias="WEBULL_APP_KEY", repr=False)
    webull_app_secret: str | None = Field(default=None, alias="WEBULL_APP_SECRET", repr=False)
    webull_paper_app_key: str | None = Field(default=None, alias="WEBULL_PAPER_APP_KEY", repr=False)
    webull_paper_app_secret: str | None = Field(default=None, alias="WEBULL_PAPER_APP_SECRET", repr=False)
    webull_live_app_key: str | None = Field(default=None, alias="WEBULL_LIVE_APP_KEY", repr=False)
    webull_live_app_secret: str | None = Field(default=None, alias="WEBULL_LIVE_APP_SECRET", repr=False)
    webull_trading_mode: Literal["paper", "live"] = Field(default="paper", alias="WEBULL_TRADING_MODE")
    webull_endpoint: str = Field(default="", alias="WEBULL_ENDPOINT")

    @model_validator(mode="after")
    def validate_webull_environment(self):
        self.webull_endpoint = resolve_webull_endpoint(self.webull_trading_mode, self.webull_endpoint)
        return self
