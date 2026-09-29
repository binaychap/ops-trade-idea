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


class WebullSettings(BaseSettings):
    webull_trading_mode: Literal["paper", "live"] = Field(default="paper", alias="WEBULL_TRADING_MODE")
    webull_endpoint: str = Field(default="", alias="WEBULL_ENDPOINT")

    @model_validator(mode="after")
    def validate_webull_environment(self):
        self.webull_endpoint = resolve_webull_endpoint(self.webull_trading_mode, self.webull_endpoint)
        return self
